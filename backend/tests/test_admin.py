from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.bootstrap_admin import bootstrap_platform_admin
from app.dependencies import platform_admin_user
from app.models import AuditEvent, AuthSession, User, Workspace
from app.problems import ProblemException
from app.routers.admin import list_all_workspaces, update_workspace_quotas
from app.schemas import WorkspaceQuotaUpdate
from app.security import token_hash, utcnow, verify_password


def test_platform_admin_bootstrap_is_idempotent_and_does_not_reset_password(
    database, db, settings
):
    settings = settings.model_copy(
        update={
            "bootstrap_admin_email": "platform@example.org",
            "bootstrap_admin_password": SecretStr("initial secure admin password"),
        }
    )
    created = bootstrap_platform_admin(settings, database)
    unchanged = bootstrap_platform_admin(settings, database)

    assert created.status == "created"
    assert unchanged.status == "unchanged"
    db.expire_all()
    user = db.scalar(select(User).where(User.normalized_email == "platform@example.org"))
    assert user is not None
    assert user.is_platform_admin and user.is_active and user.email_verified_at
    assert verify_password(user.password_hash, "initial secure admin password")
    assert (
        db.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "platform_admin_bootstrap"
            )
        )
        == 1
    )


def test_admin_api_requires_totp_verified_session_and_audits_quota_change(db, settings):
    admin = User(
        email="admin@example.org",
        normalized_email="admin@example.org",
        password_hash="unused",
        email_verified_at=utcnow(),
        is_platform_admin=True,
    )
    workspace = Workspace(name="Tenant", slug="tenant-admin-test")
    db.add_all([admin, workspace])
    db.flush()
    auth_session = AuthSession(
        user_id=admin.id,
        user=admin,
        token_hash=token_hash(settings, "session"),
        csrf_hash=token_hash(settings, "csrf"),
        expires_at=utcnow() + timedelta(hours=1),
    )

    with pytest.raises(ProblemException) as error:
        platform_admin_user(auth_session, settings)
    assert error.value.code == "platform_admin_mfa_required"

    admin.totp_secret_encrypted = "configured"
    auth_session.mfa_verified_at = utcnow()
    assert platform_admin_user(auth_session, settings) is admin

    result = update_workspace_quotas(
        workspace.id,
        WorkspaceQuotaUpdate(profile_quota=25, member_quota=100),
        admin,
        db,
        None,
    )
    assert (result.profile_quota, result.member_quota) == (25, 100)
    listing = list_all_workspaces(admin, db, None, 50, 0)
    assert listing.total == 1 and listing.items[0].id == workspace.id
    audit = db.scalar(
        select(AuditEvent).where(AuditEvent.action == "workspace_quotas_updated")
    )
    assert audit is not None
    assert audit.metadata_json["previous"] == {
        "profile_quota": 3,
        "member_quota": 10,
    }


def test_platform_admin_mfa_claim_must_be_recent(db, settings):
    admin = User(
        email="stale-admin@example.org",
        normalized_email="stale-admin@example.org",
        password_hash="unused",
        email_verified_at=utcnow(),
        is_platform_admin=True,
        totp_secret_encrypted="configured",
    )
    session = AuthSession(
        user=admin,
        token_hash=token_hash(settings, "stale-session"),
        csrf_hash=token_hash(settings, "stale-csrf"),
        expires_at=utcnow() + timedelta(hours=1),
        mfa_verified_at=utcnow() - timedelta(minutes=6),
    )
    db.add_all([admin, session])
    db.commit()
    short_window = settings.model_copy(update={"admin_mfa_max_age_seconds": 300})

    with pytest.raises(ProblemException) as error:
        platform_admin_user(session, short_window)

    assert error.value.code == "platform_admin_mfa_stale"


def test_bootstrap_promotion_revokes_existing_sessions(database, db, settings):
    user = User(
        email="promote@example.org",
        normalized_email="promote@example.org",
        password_hash="existing-password-hash",
        email_verified_at=utcnow(),
    )
    db.add(user)
    db.flush()
    session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(settings, "promotion-session"),
        csrf_hash=token_hash(settings, "promotion-csrf"),
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(session)
    db.commit()
    settings = settings.model_copy(
        update={
            "bootstrap_admin_email": user.email,
            "bootstrap_admin_password": SecretStr("bootstrap password remains unused"),
        }
    )

    result = bootstrap_platform_admin(settings, database)

    assert result.status == "updated"
    db.expire_all()
    assert db.get(User, user.id).is_platform_admin
    assert db.get(AuthSession, session.id).revoked_at is not None
    audit = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "platform_admin_bootstrap",
            AuditEvent.actor_user_id == user.id,
        )
    )
    assert audit.metadata_json["sessions_revoked"] is True
