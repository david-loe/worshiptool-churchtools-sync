from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..dependencies import (
    AuthSessionDep,
    CsrfDep,
    CurrentUserDep,
    DbDep,
    SettingsDep,
    set_request_user_context,
)
from ..models import (
    AuthSession,
    Membership,
    OneTimeToken,
    TokenPurpose,
    User,
    Workspace,
    WorkspaceRole,
)
from ..outbox import enqueue_email, link_email_body
from ..problems import ProblemException
from ..rate_limit import enforce_auth_rate_limit
from ..schemas import (
    LoginRequest,
    RecoveryConfirmRequest,
    RecoveryRequest,
    RecoveryRequestedResponse,
    RegisterRequest,
    RegisterResponse,
    SessionResponse,
    TotpConfirmRequest,
    TotpConfirmResponse,
    TotpDisableRequest,
    TotpSetupRequest,
    TotpSetupResponse,
    UserOut,
    VerificationRequestedResponse,
    VerificationResendRequest,
    VerifyEmailRequest,
)
from ..security import (
    PasswordHashCapacityError,
    SecretCipher,
    generate_recovery_codes,
    generate_totp_secret,
    hash_password,
    hash_recovery_codes,
    issue_session_tokens,
    normalize_email,
    password_needs_rehash,
    random_token,
    token_hash,
    totp_uri,
    utcnow,
    verify_and_consume_recovery_code,
    verify_password,
    verify_totp,
)


router = APIRouter(prefix="/auth", tags=["Authentifizierung"])


def _password_capacity_problem() -> ProblemException:
    return ProblemException(
        503,
        "Passwortprüfung ausgelastet",
        "Die sichere Passwortprüfung ist gerade ausgelastet. Bitte versuche es gleich erneut.",
        "password_hash_capacity_exceeded",
        headers={"Retry-After": "1"},
    )


def _hash_password(settings: SettingsDep, value: str) -> str:
    try:
        return hash_password(
            value,
            max_concurrency=settings.password_hash_max_concurrency,
            acquire_timeout=settings.password_hash_acquire_timeout_seconds,
        )
    except PasswordHashCapacityError as exc:
        raise _password_capacity_problem() from exc


def _verify_password(settings: SettingsDep, password_hash: str, candidate: str) -> bool:
    try:
        return verify_password(
            password_hash,
            candidate,
            max_concurrency=settings.password_hash_max_concurrency,
            acquire_timeout=settings.password_hash_acquire_timeout_seconds,
        )
    except PasswordHashCapacityError as exc:
        raise _password_capacity_problem() from exc


def _locked_user(db: DbDep, user_id: uuid.UUID) -> User:
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ProblemException(
            401, "Nicht angemeldet", "Das Konto existiert nicht.", "invalid_session"
        )
    return user


def _verify_current_factor(
    user: User,
    settings: SettingsDep,
    *,
    code: SecretStr | None,
    recovery_code: SecretStr | None,
) -> bool:
    if user.totp_secret_encrypted is None:
        return False
    if code is not None:
        secret = SecretCipher(settings).decrypt_text(
            user.totp_secret_encrypted, context=f"user:{user.id}:totp"
        )
        return verify_totp(secret, code.get_secret_value())
    if recovery_code is not None:
        accepted, remaining = verify_and_consume_recovery_code(
            settings,
            user.totp_recovery_hashes,
            recovery_code.get_secret_value(),
        )
        if accepted:
            user.totp_recovery_hashes = remaining
        return accepted
    return False


def _revoke_other_sessions(
    db: DbDep, user_id: uuid.UUID, current_session_id: uuid.UUID, now: datetime
) -> None:
    db.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.id != current_session_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        email_verified_at=user.email_verified_at,
        is_platform_admin=user.is_platform_admin,
        email_verified=user.email_verified_at is not None,
        totp_enabled=user.totp_secret_encrypted is not None,
    )


def _slug(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:55]
    return f"{normalized or 'workspace'}-{uuid.uuid4().hex[:8]}"


def _new_one_time_token(
    db: DbDep,
    settings: SettingsDep,
    user: User,
    purpose: TokenPurpose,
    ttl_seconds: int,
) -> str:
    raw_token = random_token()
    db.add(
        OneTimeToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=token_hash(settings, raw_token),
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
    )
    return raw_token


def _enqueue_verification_email(
    db: DbDep,
    settings: SettingsDep,
    user: User,
    workspace_id: uuid.UUID | None,
    raw_token: str,
) -> None:
    verification_url = (
        f"{settings.public_base_url.rstrip('/')}/email-bestaetigen?token={raw_token}"
    )
    text_body, html_body = link_email_body(
        "E-Mail-Adresse bestätigen",
        "Bestätige deine E-Mail-Adresse, um dein Konto zu aktivieren.",
        verification_url,
    )
    enqueue_email(
        db,
        settings,
        recipient=user.email,
        subject="E-Mail-Adresse bestätigen",
        text=text_body,
        html_body=html_body,
        workspace_id=workspace_id,
        idempotency_key=(
            f"verify-email:{user.id}:{token_hash(settings, raw_token)[:16]}"
        ),
    )


def _set_session_cookies(
    response: Response,
    settings: SettingsDep,
    raw_session: str,
    raw_csrf: str,
) -> None:
    common = {
        "secure": settings.cookie_secure,
        "samesite": "strict",
        "path": "/",
        "max_age": settings.session_ttl_seconds,
    }
    response.set_cookie(
        settings.session_cookie_name,
        raw_session,
        httponly=True,
        **common,
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        raw_csrf,
        httponly=False,
        **common,
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    settings: SettingsDep,
    db: DbDep,
    request: Request = None,
) -> RegisterResponse:
    if not settings.registration_enabled:
        raise ProblemException(
            403,
            "Registrierung deaktiviert",
            "Auf dieser Instanz sind keine offenen Registrierungen möglich.",
            "registration_disabled",
        )
    normalized_email = normalize_email(str(payload.email))
    enforce_auth_rate_limit(
        request, settings, "register", identity=normalized_email
    )
    if db.scalar(select(User.id).where(User.normalized_email == normalized_email)):
        raise ProblemException(
            409,
            "E-Mail bereits registriert",
            "Für diese E-Mail-Adresse besteht bereits ein Konto.",
            "email_already_registered",
        )
    user = User(
        email=str(payload.email).strip(),
        normalized_email=normalized_email,
        password_hash=_hash_password(settings, payload.password.get_secret_value()),
    )
    if not settings.require_email_verification:
        user.email_verified_at = utcnow()
    # The new account is the tenant identity for its initial workspace. Flush
    # it first so PostgreSQL RLS can validate the subsequent workspace and
    # initial-owner inserts without granting an unauthenticated bypass.
    db.add(user)
    db.flush()
    set_request_user_context(db, user.id)
    workspace = Workspace(name=payload.workspace_name.strip(), slug=_slug(payload.workspace_name))
    db.add(workspace)
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
    )
    # The account does not have an authenticated session yet, but its
    # verification e-mail is still tenant-scoped by the same transaction-local
    # identity used by authenticated requests.
    db.flush()
    raw_token = None
    if settings.require_email_verification:
        raw_token = _new_one_time_token(
            db,
            settings,
            user,
            TokenPurpose.VERIFY_EMAIL,
            settings.verification_ttl_seconds,
        )
        _enqueue_verification_email(db, settings, user, workspace.id, raw_token)
    db.commit()
    return RegisterResponse(
        user=_user_out(user),
        workspace_id=workspace.id,
        verification_required=settings.require_email_verification,
        development_verification_token=(
            raw_token if settings.expose_development_tokens else None
        ),
    )


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
def verify_email(
    payload: VerifyEmailRequest,
    settings: SettingsDep,
    db: DbDep,
    request: Request = None,
) -> Response:
    now = utcnow()
    raw_token = payload.token.get_secret_value()
    enforce_auth_rate_limit(
        request, settings, "verification", identity=f"token:{raw_token}"
    )
    token = db.scalar(
        select(OneTimeToken).where(
            OneTimeToken.token_hash
            == token_hash(settings, raw_token),
            OneTimeToken.purpose == TokenPurpose.VERIFY_EMAIL,
            OneTimeToken.consumed_at.is_(None),
        ).with_for_update()
    )
    now = utcnow()
    if token is None or _as_utc(token.expires_at) <= now:
        raise ProblemException(
            400,
            "Ungültiger Link",
            "Der Verifizierungslink ist ungültig oder abgelaufen.",
            "invalid_verification_token",
        )
    user = db.get(User, token.user_id)
    if user is None:
        raise ProblemException(400, "Ungültiger Link", "Das Konto existiert nicht.", "invalid_token")
    enforce_auth_rate_limit(
        request,
        settings,
        "verification",
        identity=user.normalized_email,
        include_ip=False,
    )
    user.email_verified_at = now
    token.consumed_at = now
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/verification/request",
    response_model=VerificationRequestedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_verification(
    payload: VerificationResendRequest,
    settings: SettingsDep,
    db: DbDep,
    request: Request = None,
) -> VerificationRequestedResponse:
    """Request a fresh verification message without revealing account state."""

    normalized_email = normalize_email(str(payload.email))
    enforce_auth_rate_limit(
        request, settings, "verification", identity=normalized_email
    )
    user = db.scalar(
        select(User)
        .where(User.normalized_email == normalized_email)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or not user.is_active
        or user.email_verified_at is not None
        or not settings.require_email_verification
    ):
        db.commit()
        return VerificationRequestedResponse()

    set_request_user_context(db, user.id)
    now = utcnow()
    latest = db.scalar(
        select(OneTimeToken)
        .where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.VERIFY_EMAIL,
        )
        .order_by(OneTimeToken.created_at.desc(), OneTimeToken.id.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        latest is not None
        and latest.consumed_at is None
        and _as_utc(latest.expires_at) > now
        and _as_utc(latest.created_at)
        + timedelta(seconds=settings.verification_resend_cooldown_seconds)
        > now
    ):
        # A generic 202 for both real and unknown accounts prevents the
        # cooldown itself from becoming an enumeration oracle.
        db.commit()
        return VerificationRequestedResponse()

    db.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.VERIFY_EMAIL,
            OneTimeToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    raw_token = _new_one_time_token(
        db,
        settings,
        user,
        TokenPurpose.VERIFY_EMAIL,
        settings.verification_ttl_seconds,
    )
    workspace_id = db.scalar(
        select(Membership.workspace_id)
        .where(Membership.user_id == user.id)
        .order_by(Membership.created_at, Membership.id)
        .limit(1)
    )
    _enqueue_verification_email(db, settings, user, workspace_id, raw_token)
    db.commit()
    return VerificationRequestedResponse(
        development_verification_token=(
            raw_token if settings.expose_development_tokens else None
        )
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@router.post("/login", response_model=SessionResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    db: DbDep,
) -> SessionResponse:
    enforce_auth_rate_limit(
        request,
        settings,
        "login",
        identity=normalize_email(str(payload.email)),
    )
    user = db.scalar(
        select(User)
        .where(User.normalized_email == normalize_email(str(payload.email)))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active or not _verify_password(
        settings, user.password_hash, payload.password.get_secret_value()
    ):
        raise ProblemException(
            401,
            "Anmeldung fehlgeschlagen",
            "E-Mail-Adresse oder Passwort ist nicht korrekt.",
            "invalid_credentials",
        )
    if settings.require_email_verification and user.email_verified_at is None:
        raise ProblemException(
            403,
            "E-Mail nicht bestätigt",
            "Bitte bestätige zuerst deine E-Mail-Adresse.",
            "email_not_verified",
        )
    mfa_verified = False
    if user.totp_secret_encrypted:
        accepted = False
        if payload.totp_code:
            secret = SecretCipher(settings).decrypt_text(
                user.totp_secret_encrypted, context=f"user:{user.id}:totp"
            )
            accepted = verify_totp(secret, payload.totp_code.get_secret_value())
        elif payload.recovery_code:
            accepted, remaining = verify_and_consume_recovery_code(
                settings,
                user.totp_recovery_hashes,
                payload.recovery_code.get_secret_value(),
            )
            if accepted:
                user.totp_recovery_hashes = remaining
        if not accepted:
            raise ProblemException(
                401,
                "Zweiter Faktor erforderlich",
                "Ein gültiger TOTP- oder Wiederherstellungscode ist erforderlich.",
                "mfa_required",
            )
        mfa_verified = True
    if password_needs_rehash(user.password_hash):
        user.password_hash = _hash_password(
            settings, payload.password.get_secret_value()
        )
    issued = issue_session_tokens(settings)
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(settings, issued.session_token),
        csrf_hash=token_hash(settings, issued.csrf_token),
        expires_at=issued.expires_at,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        ip_address=request.client.host[:64] if request.client else None,
        mfa_verified_at=utcnow() if mfa_verified else None,
    )
    db.add(auth_session)
    db.commit()
    _set_session_cookies(
        response, settings, issued.session_token, issued.csrf_token
    )
    return SessionResponse(user=_user_out(user), csrf_token=issued.csrf_token)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUserDep) -> UserOut:
    return _user_out(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    settings: SettingsDep,
    db: DbDep,
    auth_session: AuthSessionDep,
    csrf: CsrfDep,
) -> Response:
    auth_session.revoked_at = utcnow()
    db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/recovery/request", response_model=RecoveryRequestedResponse, status_code=202)
def request_recovery(
    payload: RecoveryRequest,
    settings: SettingsDep,
    db: DbDep,
    request: Request = None,
) -> RecoveryRequestedResponse:
    normalized_email = normalize_email(str(payload.email))
    enforce_auth_rate_limit(
        request, settings, "recovery", identity=normalized_email
    )
    user = db.scalar(
        select(User)
        .where(User.normalized_email == normalized_email)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    raw_token = None
    if user is not None and user.is_active:
        set_request_user_context(db, user.id)
        raw_token = _new_one_time_token(
            db,
            settings,
            user,
            TokenPurpose.RECOVER_PASSWORD,
            settings.recovery_ttl_seconds,
        )
        recovery_url = (
            f"{settings.public_base_url.rstrip('/')}/passwort-zuruecksetzen?token={raw_token}"
        )
        text_body, html_body = link_email_body(
            "Passwort zurücksetzen",
            "Über diesen Link kannst du ein neues Passwort vergeben.",
            recovery_url,
        )
        workspace_id = db.scalar(
            select(Membership.workspace_id)
            .where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
            .limit(1)
        )
        enqueue_email(
            db,
            settings,
            recipient=user.email,
            subject="Passwort zurücksetzen",
            text=text_body,
            html_body=html_body,
            workspace_id=workspace_id,
            idempotency_key=f"password-recovery:{user.id}:{token_hash(settings, raw_token)[:16]}",
        )
        db.commit()
    return RecoveryRequestedResponse(
        development_recovery_token=(
            raw_token if settings.expose_development_tokens else None
        )
    )


@router.post("/recovery/confirm", status_code=status.HTTP_204_NO_CONTENT)
def confirm_recovery(
    payload: RecoveryConfirmRequest,
    settings: SettingsDep,
    db: DbDep,
    request: Request = None,
) -> Response:
    raw_token = payload.token.get_secret_value()
    enforce_auth_rate_limit(
        request, settings, "recovery", identity=f"token:{raw_token}"
    )
    preliminary = db.scalar(
        select(OneTimeToken).where(
            OneTimeToken.token_hash
            == token_hash(settings, raw_token),
            OneTimeToken.purpose == TokenPurpose.RECOVER_PASSWORD,
            OneTimeToken.consumed_at.is_(None),
        )
    )
    now = utcnow()
    if preliminary is None or _as_utc(preliminary.expires_at) <= now:
        raise ProblemException(
            400,
            "Ungültiger Link",
            "Der Wiederherstellungslink ist ungültig oder abgelaufen.",
            "invalid_recovery_token",
        )
    # Lock the shared user row before the individual token row. Every password
    # reset for this account therefore follows the same lock order and cannot
    # deadlock while consuming all sibling recovery tokens.
    user = db.scalar(
        select(User)
        .where(User.id == preliminary.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ProblemException(400, "Ungültiger Link", "Das Konto existiert nicht.", "invalid_token")
    token = db.scalar(
        select(OneTimeToken).where(
            OneTimeToken.id == preliminary.id,
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.RECOVER_PASSWORD,
            OneTimeToken.consumed_at.is_(None),
        ).with_for_update()
    )
    now = utcnow()
    if token is None or _as_utc(token.expires_at) <= now:
        raise ProblemException(
            400,
            "Ungültiger Link",
            "Der Wiederherstellungslink ist ungültig oder abgelaufen.",
            "invalid_recovery_token",
        )
    enforce_auth_rate_limit(
        request,
        settings,
        "recovery",
        identity=user.normalized_email,
        include_ip=False,
    )
    user.password_hash = _hash_password(
        settings, payload.new_password.get_secret_value()
    )
    db.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == TokenPurpose.RECOVER_PASSWORD,
            OneTimeToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/totp/setup", response_model=TotpSetupResponse)
def setup_totp(
    payload: TotpSetupRequest,
    user: CurrentUserDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
    request: Request = None,
) -> TotpSetupResponse:
    enforce_auth_rate_limit(
        request, settings, "totp", identity=user.normalized_email
    )
    locked_user = _locked_user(db, user.id)
    if not _verify_password(
        settings,
        locked_user.password_hash,
        payload.password.get_secret_value(),
    ):
        raise ProblemException(
            400,
            "Prüfung fehlgeschlagen",
            "Passwort oder zweiter Faktor ist ungültig.",
            "invalid_mfa_confirmation",
        )
    if locked_user.totp_secret_encrypted is not None and not _verify_current_factor(
        locked_user,
        settings,
        code=payload.code,
        recovery_code=payload.recovery_code,
    ):
        raise ProblemException(
            400,
            "Prüfung fehlgeschlagen",
            "Passwort oder zweiter Faktor ist ungültig.",
            "invalid_mfa_confirmation",
        )
    secret = generate_totp_secret()
    locked_user.totp_pending_secret_encrypted = SecretCipher(settings).encrypt_text(
        secret, context=f"user:{locked_user.id}:totp-pending"
    )
    db.commit()
    return TotpSetupResponse(
        secret=secret, provisioning_uri=totp_uri(secret, locked_user.email)
    )


@router.post("/totp/confirm", response_model=TotpConfirmResponse)
def confirm_totp(
    payload: TotpConfirmRequest,
    user: CurrentUserDep,
    auth_session: AuthSessionDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
    request: Request = None,
) -> TotpConfirmResponse:
    enforce_auth_rate_limit(
        request, settings, "totp", identity=user.normalized_email
    )
    locked_user = _locked_user(db, user.id)
    if locked_user.totp_pending_secret_encrypted is None:
        raise ProblemException(
            409,
            "Keine Einrichtung aktiv",
            "Starte zuerst die TOTP-Einrichtung.",
            "totp_setup_missing",
        )
    cipher = SecretCipher(settings)
    secret = cipher.decrypt_text(
        locked_user.totp_pending_secret_encrypted,
        context=f"user:{locked_user.id}:totp-pending",
    )
    if not verify_totp(secret, payload.code.get_secret_value()):
        raise ProblemException(400, "Ungültiger Code", "Der TOTP-Code ist ungültig.", "invalid_totp")
    recovery_codes = generate_recovery_codes()
    locked_user.totp_secret_encrypted = cipher.encrypt_text(
        secret, context=f"user:{locked_user.id}:totp"
    )
    locked_user.totp_pending_secret_encrypted = None
    locked_user.totp_recovery_hashes = hash_recovery_codes(settings, recovery_codes)
    now = utcnow()
    auth_session.mfa_verified_at = now
    _revoke_other_sessions(db, locked_user.id, auth_session.id, now)
    db.commit()
    return TotpConfirmResponse(enabled=True, recovery_codes=recovery_codes)


@router.post("/totp/disable", status_code=status.HTTP_204_NO_CONTENT)
def disable_totp(
    payload: TotpDisableRequest,
    user: CurrentUserDep,
    auth_session: AuthSessionDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
    request: Request = None,
) -> Response:
    enforce_auth_rate_limit(
        request, settings, "totp", identity=user.normalized_email
    )
    locked_user = _locked_user(db, user.id)
    if locked_user.is_platform_admin:
        raise ProblemException(
            409,
            "TOTP erforderlich",
            "Plattform-Administratoren müssen TOTP aktiviert lassen.",
            "platform_admin_mfa_required",
        )
    if (
        locked_user.totp_secret_encrypted is None
        or not _verify_password(
            settings,
            locked_user.password_hash,
            payload.password.get_secret_value(),
        )
        or not _verify_current_factor(
            locked_user,
            settings,
            code=payload.code,
            recovery_code=payload.recovery_code,
        )
    ):
        raise ProblemException(
            400,
            "Prüfung fehlgeschlagen",
            "Passwort oder zweiter Faktor ist ungültig.",
            "invalid_mfa_confirmation",
        )
    now = utcnow()
    locked_user.totp_secret_encrypted = None
    locked_user.totp_pending_secret_encrypted = None
    locked_user.totp_recovery_hashes = []
    auth_session.mfa_verified_at = None
    _revoke_other_sessions(db, locked_user.id, auth_session.id, now)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
