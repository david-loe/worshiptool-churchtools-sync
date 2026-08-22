from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pyotp
import pytest
from fastapi import Response
from pydantic import SecretStr
from sqlalchemy import select
from starlette.requests import Request

from app.config import Settings
from app.dependencies import WorkspaceAccess
from app.models import (
    AuthSession,
    Membership,
    NotificationOutbox,
    OneTimeToken,
    TokenPurpose,
    User,
    Workspace,
    WorkspaceRole,
)
from app.problems import ProblemException
from app.outbox import DeliveryError, VapidPushSender
from app.rate_limit import (
    InMemoryFixedWindowRateLimiter,
    RateLimitBackendUnavailable,
    RateLimitResult,
    default_rate_limiter,
    enforce_auth_rate_limit,
)
from app.routers.auth import (
    confirm_totp,
    confirm_recovery,
    disable_totp,
    login,
    register,
    request_recovery,
    request_verification,
    setup_totp,
    verify_email,
)
from app.routers.notifications import register_push_subscription
from app.routers.workspaces import (
    create_workspace,
    invite_member,
    update_member_role,
)
from app.schemas import (
    InvitationCreate,
    MemberRoleUpdate,
    PushSubscriptionCreate,
    LoginRequest,
    RecoveryConfirmRequest,
    RecoveryRequest,
    RegisterRequest,
    TotpConfirmRequest,
    TotpDisableRequest,
    TotpSetupRequest,
    VerificationResendRequest,
    VerifyEmailRequest,
    WorkspaceCreate,
)
from app.security import (
    PasswordHashCapacityError,
    SecretCipher,
    hash_password,
    hash_recovery_codes,
    token_hash,
    utcnow,
    verify_password,
)


def _request(limiter, ip: str = "203.0.113.42") -> Request:
    app = SimpleNamespace(state=SimpleNamespace(rate_limiter=limiter))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("sync.example.org", 443),
            "client": (ip, 49152),
            "app": app,
        }
    )


def _user(email: str) -> User:
    return User(
        email=email,
        normalized_email=email.casefold(),
        password_hash=hash_password("correct horse battery staple"),
        email_verified_at=utcnow(),
    )


def test_fixed_window_rate_limit_uses_ip_and_hashed_identity(settings):
    limiter = InMemoryFixedWindowRateLimiter(clock=lambda: 120.0)
    request = _request(limiter)

    for _ in range(3):
        enforce_auth_rate_limit(
            request,
            settings,
            "register",
            identity="person@example.org",
        )

    with pytest.raises(ProblemException) as error:
        enforce_auth_rate_limit(
            request,
            settings,
            "register",
            identity="person@example.org",
        )

    assert error.value.status == 429
    assert error.value.code == "rate_limit_exceeded"
    assert int(error.value.headers["Retry-After"]) > 0


def test_rate_limit_keys_do_not_contain_ip_or_email(settings):
    class RecordingLimiter:
        def __init__(self):
            self.keys = []

        def check(self, key, *, limit, window_seconds):
            self.keys.append(key)
            return RateLimitResult(True, window_seconds)

    limiter = RecordingLimiter()
    enforce_auth_rate_limit(
        _request(limiter),
        settings,
        "login",
        identity="private.person@example.org",
    )

    assert len(limiter.keys) == 2
    assert all("203.0.113.42" not in key for key in limiter.keys)
    assert all("private.person@example.org" not in key for key in limiter.keys)


def test_rate_limiter_is_redis_independent_in_tests_and_fails_closed_in_production(
    settings,
):
    assert isinstance(default_rate_limiter(settings), InMemoryFixedWindowRateLimiter)

    class UnavailableLimiter:
        def check(self, key, *, limit, window_seconds):
            raise RateLimitBackendUnavailable

    production = Settings(
        environment="production",
        database_url="postgresql+psycopg://user:password@db/app",
        public_base_url="https://sync.example.org",
        application_secret="production-application-secret-with-at-least-32-bytes",
        encryption_secret="production-encryption-secret-with-at-least-32-bytes",
        smtp_host="smtp.example.org",
    )
    with pytest.raises(ProblemException) as error:
        enforce_auth_rate_limit(
            _request(UnavailableLimiter()),
            production,
            "login",
            identity="person@example.org",
        )

    assert error.value.status == 503
    assert error.value.code == "rate_limiter_unavailable"
    assert error.value.headers == {"Retry-After": "5"}


def test_all_sensitive_auth_entrypoints_enforce_their_identity_limit(db, settings):
    register_request = _request(
        InMemoryFixedWindowRateLimiter(clock=lambda: 120.0)
    )
    registration_payload = RegisterRequest(
        email="rate@example.org",
        password="correct horse battery staple",
        workspace_name="Rate",
    )
    register(registration_payload, settings, db, register_request)
    for _ in range(2):
        with pytest.raises(ProblemException) as duplicate:
            register(registration_payload, settings, db, register_request)
        assert duplicate.value.code == "email_already_registered"
    with pytest.raises(ProblemException) as limited_register:
        register(registration_payload, settings, db, register_request)
    assert limited_register.value.code == "rate_limit_exceeded"

    login_request = _request(InMemoryFixedWindowRateLimiter(clock=lambda: 120.0))
    for _ in range(10):
        with pytest.raises(ProblemException) as invalid_login:
            login(
                LoginRequest(email="missing@example.org", password="wrong"),
                login_request,
                Response(),
                settings,
                db,
            )
        assert invalid_login.value.code == "invalid_credentials"
    with pytest.raises(ProblemException) as limited_login:
        login(
            LoginRequest(email="MISSING@example.org", password="wrong"),
            login_request,
            Response(),
            settings,
            db,
        )
    assert limited_login.value.code == "rate_limit_exceeded"

    recovery_request = _request(
        InMemoryFixedWindowRateLimiter(clock=lambda: 120.0)
    )
    for _ in range(3):
        request_recovery(
            RecoveryRequest(email="unknown@example.org"),
            settings,
            db,
            recovery_request,
        )
    with pytest.raises(ProblemException) as limited_recovery:
        request_recovery(
            RecoveryRequest(email="UNKNOWN@example.org"),
            settings,
            db,
            recovery_request,
        )
    assert limited_recovery.value.code == "rate_limit_exceeded"

    verification_request = _request(
        InMemoryFixedWindowRateLimiter(clock=lambda: 120.0)
    )
    for _ in range(10):
        with pytest.raises(ProblemException) as invalid_verification:
            verify_email(
                VerifyEmailRequest(token="invalid-token"),
                settings,
                db,
                verification_request,
            )
        assert invalid_verification.value.code == "invalid_verification_token"
    with pytest.raises(ProblemException) as limited_verification:
        verify_email(
            VerifyEmailRequest(token="invalid-token"),
            settings,
            db,
            verification_request,
        )
    assert limited_verification.value.code == "rate_limit_exceeded"

    totp_user = db.scalar(
        select(User).where(User.normalized_email == "rate@example.org")
    )
    totp_request = _request(InMemoryFixedWindowRateLimiter(clock=lambda: 120.0))
    setup_payload = TotpSetupRequest(password="correct horse battery staple")
    for _ in range(10):
        setup_totp(setup_payload, totp_user, settings, db, None, totp_request)
    with pytest.raises(ProblemException) as limited_totp:
        setup_totp(setup_payload, totp_user, settings, db, None, totp_request)
    assert limited_totp.value.code == "rate_limit_exceeded"


def test_password_reset_consumes_all_tokens_and_revokes_sessions(db, settings):
    settings = settings.model_copy(update={"expose_development_tokens": True})
    registration = register(
        RegisterRequest(
            email="reset@example.org",
            password="correct horse battery staple",
            workspace_name="Reset",
        ),
        settings,
        db,
    )
    user = db.get(User, registration.user.id)
    assert user is not None
    first = request_recovery(RecoveryRequest(email=user.email), settings, db)
    second = request_recovery(RecoveryRequest(email=user.email), settings, db)
    assert first.development_recovery_token
    assert second.development_recovery_token
    session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(settings, "existing-session"),
        csrf_hash=token_hash(settings, "existing-csrf"),
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(session)
    db.commit()

    confirm_recovery(
        RecoveryConfirmRequest(
            token=first.development_recovery_token,
            new_password="a completely new secure password",
        ),
        settings,
        db,
    )

    db.expire_all()
    recovery_tokens = db.scalars(
        select(OneTimeToken).where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.RECOVER_PASSWORD,
        )
    ).all()
    assert len(recovery_tokens) == 2
    assert all(token.consumed_at is not None for token in recovery_tokens)
    assert db.get(AuthSession, session.id).revoked_at is not None
    assert verify_password(
        db.get(User, user.id).password_hash,
        "a completely new secure password",
    )
    with pytest.raises(ProblemException) as error:
        confirm_recovery(
            RecoveryConfirmRequest(
                token=second.development_recovery_token,
                new_password="another completely secure password",
            ),
            settings,
            db,
        )
    assert error.value.code == "invalid_recovery_token"


def test_verification_resend_is_generic_cooled_down_and_replaces_expired_token(
    db, settings
):
    settings = settings.model_copy(
        update={
            "require_email_verification": True,
            "expose_development_tokens": True,
        }
    )
    registration = register(
        RegisterRequest(
            email="verify-again@example.org",
            password="correct horse battery staple",
            workspace_name="Verify",
        ),
        settings,
        db,
    )
    user = db.get(User, registration.user.id)
    assert user is not None
    initial_token = db.scalar(
        select(OneTimeToken).where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.VERIFY_EMAIL,
        )
    )
    assert initial_token is not None
    assert db.query(NotificationOutbox).count() == 1

    cooled_down = request_verification(
        VerificationResendRequest(email=user.email), settings, db
    )
    unknown = request_verification(
        VerificationResendRequest(email="unknown@example.org"), settings, db
    )
    assert cooled_down.accepted and unknown.accepted
    assert cooled_down.development_verification_token is None
    assert unknown.development_verification_token is None
    assert db.query(NotificationOutbox).count() == 1

    initial_token.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    replacement = request_verification(
        VerificationResendRequest(email=user.email), settings, db
    )

    assert replacement.accepted
    assert replacement.development_verification_token
    tokens = db.scalars(
        select(OneTimeToken)
        .where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.VERIFY_EMAIL,
        )
        .order_by(OneTimeToken.created_at)
    ).all()
    assert len(tokens) == 2
    assert tokens[0].consumed_at is not None
    assert tokens[1].consumed_at is None
    assert db.query(NotificationOutbox).count() == 2

    repeated = request_verification(
        VerificationResendRequest(email=user.email), settings, db
    )
    assert repeated.development_verification_token is None
    assert db.query(NotificationOutbox).count() == 2


def test_password_hash_capacity_saturation_returns_retryable_503(
    db, settings, monkeypatch
):
    def saturated(*args, **kwargs):
        raise PasswordHashCapacityError

    monkeypatch.setattr("app.routers.auth.hash_password", saturated)
    with pytest.raises(ProblemException) as error:
        register(
            RegisterRequest(
                email="capacity@example.org",
                password="correct horse battery staple",
                workspace_name="Capacity",
            ),
            settings,
            db,
        )

    assert error.value.status == 503
    assert error.value.code == "password_hash_capacity_exceeded"
    assert error.value.headers == {"Retry-After": "1"}


def test_totp_setup_requires_step_up_to_replace_an_active_factor(db, settings):
    user = _user("totp-existing@example.org")
    user.totp_secret_encrypted = "already-configured"
    db.add(user)
    db.commit()

    with pytest.raises(ProblemException) as error:
        setup_totp(
            TotpSetupRequest(password="correct horse battery staple"),
            user,
            settings,
            db,
            None,
        )

    assert error.value.code == "invalid_mfa_confirmation"
    assert user.totp_pending_secret_encrypted is None


def test_totp_replacement_accepts_one_time_recovery_code_and_revokes_other_sessions(
    db, settings
):
    recovery_code = "ABCD-EFGH-IJKL"
    user = _user("totp-replace@example.org")
    db.add(user)
    db.flush()
    old_secret = pyotp.random_base32()
    user.totp_secret_encrypted = SecretCipher(settings).encrypt_text(
        old_secret, context=f"user:{user.id}:totp"
    )
    user.totp_recovery_hashes = hash_recovery_codes(settings, [recovery_code])
    current_session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(settings, "current-session"),
        csrf_hash=token_hash(settings, "current-csrf"),
        expires_at=utcnow() + timedelta(hours=1),
        mfa_verified_at=utcnow(),
    )
    other_session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(settings, "other-session"),
        csrf_hash=token_hash(settings, "other-csrf"),
        expires_at=utcnow() + timedelta(hours=1),
        mfa_verified_at=utcnow(),
    )
    db.add_all([current_session, other_session])
    db.commit()

    setup = setup_totp(
        TotpSetupRequest(
            password="correct horse battery staple",
            recovery_code=recovery_code,
        ),
        user,
        settings,
        db,
        None,
    )
    assert db.get(User, user.id).totp_recovery_hashes == []

    result = confirm_totp(
        TotpConfirmRequest(code=pyotp.TOTP(setup.secret).now()),
        user,
        current_session,
        settings,
        db,
        None,
    )

    assert result.enabled and len(result.recovery_codes) >= 1
    db.expire_all()
    stored_user = db.get(User, user.id)
    assert stored_user is not None
    assert stored_user.totp_secret_encrypted is not None
    assert stored_user.totp_pending_secret_encrypted is None
    assert db.get(AuthSession, current_session.id).revoked_at is None
    assert db.get(AuthSession, other_session.id).revoked_at is not None


def test_totp_disable_requires_password_consumes_recovery_and_revokes_sessions(
    db, settings
):
    recovery_code = "MNOP-QRST-UVWX"
    user = _user("totp-disable@example.org")
    db.add(user)
    db.flush()
    user.totp_secret_encrypted = SecretCipher(settings).encrypt_text(
        pyotp.random_base32(), context=f"user:{user.id}:totp"
    )
    user.totp_recovery_hashes = hash_recovery_codes(settings, [recovery_code])
    current_session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(settings, "disable-current"),
        csrf_hash=token_hash(settings, "disable-current-csrf"),
        expires_at=utcnow() + timedelta(hours=1),
        mfa_verified_at=utcnow(),
    )
    other_session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(settings, "disable-other"),
        csrf_hash=token_hash(settings, "disable-other-csrf"),
        expires_at=utcnow() + timedelta(hours=1),
        mfa_verified_at=utcnow(),
    )
    db.add_all([current_session, other_session])
    db.commit()

    disable_totp(
        TotpDisableRequest(
            password="correct horse battery staple",
            recovery_code=recovery_code,
        ),
        user,
        current_session,
        settings,
        db,
        None,
    )

    db.expire_all()
    stored_user = db.get(User, user.id)
    assert stored_user is not None
    assert stored_user.totp_secret_encrypted is None
    assert stored_user.totp_recovery_hashes == []
    assert db.get(AuthSession, current_session.id).mfa_verified_at is None
    assert db.get(AuthSession, current_session.id).revoked_at is None
    assert db.get(AuthSession, other_session.id).revoked_at is not None


def test_owner_workspace_quota_counts_only_owned_workspaces(db, settings):
    settings = settings.model_copy(update={"workspace_quota_per_user": 1})
    viewer = _user("viewer@example.org")
    shared = Workspace(name="Shared", slug=f"shared-{uuid.uuid4().hex[:8]}")
    db.add_all([viewer, shared])
    db.flush()
    db.add(
        Membership(
            workspace_id=shared.id,
            user_id=viewer.id,
            role=WorkspaceRole.VIEWER,
        )
    )
    db.commit()

    created = create_workspace(
        WorkspaceCreate(name="Owned"), viewer, settings, db, None
    )
    assert created.role == WorkspaceRole.OWNER
    with pytest.raises(ProblemException) as error:
        create_workspace(
            WorkspaceCreate(name="Too many"), viewer, settings, db, None
        )
    assert error.value.code == "workspace_quota_exceeded"


def test_owner_promotion_obeys_workspace_quota(db, settings):
    settings = settings.model_copy(update={"workspace_quota_per_user": 1})
    owner = _user("owner@example.org")
    candidate = _user("candidate@example.org")
    workspace = Workspace(name="Team", slug=f"team-{uuid.uuid4().hex[:8]}")
    existing = Workspace(name="Existing", slug=f"existing-{uuid.uuid4().hex[:8]}")
    db.add_all([owner, candidate, workspace, existing])
    db.flush()
    owner_membership = Membership(
        workspace_id=workspace.id,
        user_id=owner.id,
        role=WorkspaceRole.OWNER,
    )
    candidate_membership = Membership(
        workspace_id=workspace.id,
        user_id=candidate.id,
        role=WorkspaceRole.VIEWER,
    )
    db.add_all(
        [
            owner_membership,
            candidate_membership,
            Membership(
                workspace_id=existing.id,
                user_id=candidate.id,
                role=WorkspaceRole.OWNER,
            ),
        ]
    )
    db.commit()
    access = WorkspaceAccess(workspace, owner, WorkspaceRole.OWNER)

    with pytest.raises(ProblemException) as error:
        update_member_role(
            candidate_membership.id,
            MemberRoleUpdate(role=WorkspaceRole.OWNER),
            access,
            settings,
            db,
            None,
        )
    assert error.value.code == "workspace_quota_exceeded"


def test_member_quota_counts_active_invitations(db, settings):
    owner = _user("quota-owner@example.org")
    workspace = Workspace(
        name="Quota",
        slug=f"quota-{uuid.uuid4().hex[:8]}",
        member_quota=2,
    )
    db.add_all([owner, workspace])
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=owner.id,
            role=WorkspaceRole.OWNER,
        )
    )
    db.commit()
    access = WorkspaceAccess(workspace, owner, WorkspaceRole.OWNER)

    invite_member(
        InvitationCreate(email="first@example.org"), access, settings, db, None
    )
    with pytest.raises(ProblemException) as error:
        invite_member(
            InvitationCreate(email="second@example.org"),
            access,
            settings,
            db,
            None,
        )
    assert error.value.code == "member_quota_exceeded"


def test_invitation_resend_has_cooldown_and_does_not_duplicate_mail(db, settings):
    owner = _user("cooldown-owner@example.org")
    workspace = Workspace(
        name="Cooldown",
        slug=f"cooldown-{uuid.uuid4().hex[:8]}",
    )
    db.add_all([owner, workspace])
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=owner.id,
            role=WorkspaceRole.OWNER,
        )
    )
    db.commit()
    access = WorkspaceAccess(workspace, owner, WorkspaceRole.OWNER)

    invite_member(
        InvitationCreate(email="recipient@example.org"),
        access,
        settings,
        db,
        None,
    )
    with pytest.raises(ProblemException) as error:
        invite_member(
            InvitationCreate(email="RECIPIENT@example.org"),
            access,
            settings,
            db,
            None,
        )

    assert error.value.status == 429
    assert error.value.code == "invitation_resend_cooldown"
    assert int(error.value.headers["Retry-After"]) > 0
    assert db.query(NotificationOutbox).count() == 1


def test_invitation_rate_limit_backend_fails_closed_in_production(db, settings):
    class UnavailableLimiter:
        def check(self, key, *, limit, window_seconds):
            raise RateLimitBackendUnavailable

    owner = _user("fail-closed-owner@example.org")
    workspace = Workspace(
        name="Fail closed",
        slug=f"fail-closed-{uuid.uuid4().hex[:8]}",
    )
    db.add_all([owner, workspace])
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=owner.id,
            role=WorkspaceRole.OWNER,
        )
    )
    db.commit()
    production_like = settings.model_copy(update={"environment": "production"})

    with pytest.raises(ProblemException) as error:
        invite_member(
            InvitationCreate(email="recipient@example.org"),
            WorkspaceAccess(workspace, owner, WorkspaceRole.OWNER),
            production_like,
            db,
            None,
            _request(UnavailableLimiter()),
        )

    assert error.value.status == 503
    assert error.value.code == "rate_limiter_unavailable"
    assert db.query(NotificationOutbox).count() == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/token",
        "https://localhost/push",
        "https://127.0.0.1/push",
        "https://fcm.googleapis.com:8443/fcm/send/token",
        "https://fcm.googleapis.com.attacker.example/push",
    ],
)
def test_push_subscription_rejects_ssrf_endpoints(db, settings, endpoint):
    user = _user("push@example.org")
    workspace = Workspace(name="Push", slug=f"push-{uuid.uuid4().hex[:8]}")
    db.add_all([user, workspace])
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
    )
    db.commit()

    with pytest.raises(ProblemException) as error:
        register_push_subscription(
            PushSubscriptionCreate(
                endpoint=endpoint,
                p256dh="public-key-material",
                auth="auth-material",
            ),
            WorkspaceAccess(workspace, user, WorkspaceRole.OWNER),
            settings,
            db,
            None,
        )
    assert error.value.code in {
        "invalid_push_endpoint",
        "push_endpoint_not_allowed",
    }


def test_push_subscription_accepts_known_service_and_canonicalizes_endpoint(
    db, settings
):
    user = _user("valid-push@example.org")
    workspace = Workspace(
        name="Valid Push", slug=f"valid-push-{uuid.uuid4().hex[:8]}"
    )
    db.add_all([user, workspace])
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
    )
    db.commit()

    subscription = register_push_subscription(
        PushSubscriptionCreate(
            endpoint="https://FCM.GOOGLEAPIS.COM:443/fcm/send/token",
            p256dh="public-key-material",
            auth="auth-material",
        ),
        WorkspaceAccess(workspace, user, WorkspaceRole.OWNER),
        settings,
        db,
        None,
    )

    assert subscription.endpoint_hash
    assert "fcm/send/token" not in subscription.subscription_encrypted


def test_push_subscription_quota_is_per_user_workspace_and_allows_refresh(
    db, settings
):
    settings = settings.model_copy(
        update={"max_push_subscriptions_per_user_workspace": 1}
    )
    user = _user("push-quota@example.org")
    workspace = Workspace(
        name="Push quota", slug=f"push-quota-{uuid.uuid4().hex[:8]}"
    )
    db.add_all([user, workspace])
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
    )
    db.commit()
    access = WorkspaceAccess(workspace, user, WorkspaceRole.OWNER)
    first = PushSubscriptionCreate(
        endpoint="https://fcm.googleapis.com/fcm/send/device-one",
        p256dh="public-key-material",
        auth="auth-material",
        device_name="Browser eins",
    )

    original = register_push_subscription(first, access, settings, db, None)
    refreshed = register_push_subscription(
        first.model_copy(update={"device_name": "Browser aktualisiert"}),
        access,
        settings,
        db,
        None,
    )
    assert refreshed.id == original.id
    assert refreshed.device_name == "Browser aktualisiert"

    with pytest.raises(ProblemException) as error:
        register_push_subscription(
            PushSubscriptionCreate(
                endpoint="https://fcm.googleapis.com/fcm/send/device-two",
                p256dh="second-public-key-material",
                auth="second-auth-material",
                device_name="Browser zwei",
            ),
            access,
            settings,
            db,
            None,
        )

    assert error.value.status == 409
    assert error.value.code == "push_subscription_quota_exceeded"


def test_push_sender_rejects_legacy_unsafe_endpoint_before_network_io(settings):
    push_settings = settings.model_copy(
        update={
            "vapid_private_key": SecretStr("private-vapid-key"),
            "vapid_subject": "mailto:admin@example.org",
        }
    )

    with pytest.raises(DeliveryError) as error:
        VapidPushSender(push_settings).send(
            {
                "endpoint": "https://127.0.0.1/internal",
                "keys": {"p256dh": "public-key", "auth": "auth-secret"},
            },
            {"title": "Test", "body": "Body", "data": {}},
        )

    assert error.value.code == "push_endpoint_not_allowed"
    assert error.value.permanent
