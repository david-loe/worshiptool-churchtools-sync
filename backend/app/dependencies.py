from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, Path, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import Settings
from .models import AuthSession, Membership, User, Workspace, WorkspaceRole
from .problems import ProblemException
from .security import token_hash


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request):
    yield from request.app.state.database.session()


def get_admin_db(request: Request):
    yield from request.app.state.admin_database.session()


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]
AdminDbDep = Annotated[Session, Depends(get_admin_db)]


def set_request_user_context(db: Session, user_id: uuid.UUID) -> None:
    """Set the transaction-local tenant identity consumed by PostgreSQL RLS."""

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": str(user_id)},
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def current_auth_session(
    settings: SettingsDep,
    db: DbDep,
    request: Request,
) -> AuthSession:
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if not session_cookie:
        raise ProblemException(
            401,
            "Nicht angemeldet",
            "Für diese Anfrage ist eine Anmeldung erforderlich.",
            "authentication_required",
        )
    auth_session = db.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash(settings, session_cookie))
    )
    now = datetime.now(timezone.utc)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or _as_utc(auth_session.expires_at) <= now
        or not auth_session.user.is_active
    ):
        raise ProblemException(
            401,
            "Sitzung abgelaufen",
            "Bitte melde dich erneut an.",
            "invalid_session",
        )
    # PostgreSQL RLS policies consume transaction-local identity. SQLite
    # remains the lightweight test/development backend and has no custom GUCs.
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        set_request_user_context(db, auth_session.user_id)
    return auth_session


AuthSessionDep = Annotated[AuthSession, Depends(current_auth_session)]


def current_user(auth_session: AuthSessionDep) -> User:
    return auth_session.user


CurrentUserDep = Annotated[User, Depends(current_user)]


def require_csrf(
    settings: SettingsDep,
    auth_session: AuthSessionDep,
    request: Request,
) -> None:
    csrf_cookie = request.cookies.get(settings.csrf_cookie_name)
    csrf_header = request.headers.get(settings.csrf_header_name)
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        raise ProblemException(
            403,
            "CSRF-Prüfung fehlgeschlagen",
            "Das CSRF-Token fehlt oder ist ungültig.",
            "csrf_failed",
        )
    if not hmac.compare_digest(auth_session.csrf_hash, token_hash(settings, csrf_header)):
        raise ProblemException(
            403,
            "CSRF-Prüfung fehlgeschlagen",
            "Das CSRF-Token gehört nicht zu dieser Sitzung.",
            "csrf_failed",
        )


CsrfDep = Annotated[None, Depends(require_csrf)]


@dataclass(frozen=True)
class WorkspaceAccess:
    workspace: Workspace
    user: User
    role: WorkspaceRole


ROLE_LEVEL = {
    WorkspaceRole.VIEWER: 0,
    WorkspaceRole.OPERATOR: 1,
    WorkspaceRole.ADMIN: 2,
    WorkspaceRole.OWNER: 3,
}


def workspace_access(
    db: DbDep,
    user: CurrentUserDep,
    workspace_id: Annotated[uuid.UUID, Path()],
) -> WorkspaceAccess:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ProblemException(404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found")
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == workspace_id,
            Membership.user_id == user.id,
        )
    )
    if membership is None:
        # Deliberately return 404 to avoid leaking tenant identifiers.
        raise ProblemException(404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found")
    return WorkspaceAccess(
        workspace=workspace,
        user=user,
        role=membership.role,
    )


WorkspaceAccessDep = Annotated[WorkspaceAccess, Depends(workspace_access)]


def require_workspace_role(minimum: WorkspaceRole):
    def dependency(access: WorkspaceAccessDep) -> WorkspaceAccess:
        if ROLE_LEVEL[access.role] < ROLE_LEVEL[minimum]:
            raise ProblemException(
                403,
                "Nicht berechtigt",
                "Deine Workspace-Rolle erlaubt diese Aktion nicht.",
                "insufficient_role",
            )
        return access

    return dependency


WorkspaceAdminDep = Annotated[
    WorkspaceAccess, Depends(require_workspace_role(WorkspaceRole.ADMIN))
]
WorkspaceOperatorDep = Annotated[
    WorkspaceAccess, Depends(require_workspace_role(WorkspaceRole.OPERATOR))
]


def platform_admin_user(
    auth_session: AuthSessionDep, settings: SettingsDep
) -> User:
    user = auth_session.user
    if not user.is_platform_admin:
        raise ProblemException(
            403,
            "Nicht berechtigt",
            "Diese Aktion ist Plattform-Administratoren vorbehalten.",
            "platform_admin_required",
        )
    now = datetime.now(timezone.utc)
    mfa_verified_at = (
        _as_utc(auth_session.mfa_verified_at)
        if auth_session.mfa_verified_at is not None
        else None
    )
    if user.totp_secret_encrypted is None or mfa_verified_at is None:
        raise ProblemException(
            403,
            "MFA erforderlich",
            "Für Plattform-Administration ist eine mit TOTP bestätigte Sitzung erforderlich.",
            "platform_admin_mfa_required",
        )
    if mfa_verified_at < now - timedelta(
        seconds=settings.admin_mfa_max_age_seconds
    ):
        raise ProblemException(
            403,
            "MFA-Bestätigung abgelaufen",
            "Bestätige deine Anmeldung erneut mit TOTP, bevor du die Plattform administrierst.",
            "platform_admin_mfa_stale",
        )
    return user


PlatformAdminDep = Annotated[User, Depends(platform_admin_user)]
