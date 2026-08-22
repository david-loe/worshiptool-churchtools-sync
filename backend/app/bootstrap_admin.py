from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import select, update

from .config import Settings
from .database import Database
from .models import AuditEvent, AuthSession, User
from .security import hash_password, normalize_email, utcnow


@dataclass(frozen=True)
class BootstrapResult:
    user_id: str
    status: str


def bootstrap_platform_admin(
    settings: Settings,
    database: Database,
    *,
    reset_password: bool = False,
) -> BootstrapResult:
    if not settings.bootstrap_admin_email or settings.bootstrap_admin_password is None:
        raise ValueError(
            "WT_SYNC_BOOTSTRAP_ADMIN_EMAIL und WT_SYNC_BOOTSTRAP_ADMIN_PASSWORD sind erforderlich"
        )
    email = settings.bootstrap_admin_email.strip()
    normalized = normalize_email(email)
    password = settings.bootstrap_admin_password.get_secret_value()
    with database.session_factory() as db:
        user = db.scalar(
            select(User)
            .where(User.normalized_email == normalized)
            .with_for_update()
        )
        created = user is None
        changed = False
        revoke_sessions = False
        if user is None:
            user = User(
                email=email,
                normalized_email=normalized,
                password_hash=hash_password(
                    password,
                    max_concurrency=settings.password_hash_max_concurrency,
                    acquire_timeout=settings.password_hash_acquire_timeout_seconds,
                ),
                email_verified_at=utcnow(),
                is_active=True,
                is_platform_admin=True,
            )
            db.add(user)
            db.flush()
            changed = True
        else:
            if not user.is_platform_admin or not user.is_active:
                user.is_platform_admin = True
                user.is_active = True
                changed = True
                revoke_sessions = True
            if user.email_verified_at is None:
                user.email_verified_at = utcnow()
                changed = True
            if reset_password:
                user.password_hash = hash_password(
                    password,
                    max_concurrency=settings.password_hash_max_concurrency,
                    acquire_timeout=settings.password_hash_acquire_timeout_seconds,
                )
                changed = True
                revoke_sessions = True
        if changed:
            if revoke_sessions:
                db.execute(
                    update(AuthSession)
                    .where(
                        AuthSession.user_id == user.id,
                        AuthSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utcnow())
                )
            db.add(
                AuditEvent(
                    actor_user_id=user.id,
                    action="platform_admin_bootstrap",
                    entity_type="user",
                    entity_id=str(user.id),
                    metadata_json={
                        "created": created,
                        "password_reset": bool(reset_password and not created),
                        "sessions_revoked": revoke_sessions,
                    },
                )
            )
            db.commit()
            status = "created" if created else "updated"
        else:
            status = "unchanged"
        return BootstrapResult(user_id=str(user.id), status=status)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create or promote the configured platform administrator."
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Reset an existing account to WT_SYNC_BOOTSTRAP_ADMIN_PASSWORD.",
    )
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings)
    try:
        result = bootstrap_platform_admin(
            settings, database, reset_password=args.reset_password
        )
        print(f"platform-admin {result.status}: {result.user_id}")
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
