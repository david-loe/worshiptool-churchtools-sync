from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import func, select, update

from ..dependencies import (
    CsrfDep,
    CurrentUserDep,
    DbDep,
    SettingsDep,
    WorkspaceAccessDep,
    WorkspaceAdminDep,
)
from ..models import (
    Membership,
    NotificationOutbox,
    SyncProfile,
    SyncRun,
    SyncRunStatus,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceRole,
)
from ..outbox import enqueue_email, link_email_body
from ..problems import ProblemException
from ..rate_limit import enforce_auth_rate_limit
from ..schemas import (
    MemberOut,
    MemberRoleUpdate,
    InvitationAccept,
    InvitationCreate,
    InvitationOut,
    WorkspaceCreate,
    WorkspaceList,
    WorkspaceOut,
    WorkspaceUpdate,
)
from ..security import normalize_email, random_token, token_hash


router = APIRouter(prefix="/workspaces", tags=["Workspaces"])


def _slug(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:55]
    return f"{normalized or 'workspace'}-{uuid.uuid4().hex[:8]}"


def _workspace_out(workspace: Workspace, role: WorkspaceRole) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        archived_at=workspace.archived_at,
        profile_quota=workspace.profile_quota,
        member_quota=workspace.member_quota,
        role=role,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def _lock_workspace(db: DbDep, workspace_id: uuid.UUID) -> Workspace:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ProblemException(
            404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found"
        )
    return workspace


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _owned_workspace_count(db: DbDep, user_id: uuid.UUID) -> int:
    # PostgreSQL membership RLS deliberately hides the target user's other
    # tenants from an acting owner.  The SECURITY-DEFINER aggregate returns
    # only the cross-tenant count required for the global ownership quota.
    if db.get_bind().dialect.name == "postgresql":
        return int(
            db.scalar(select(func.app_owned_workspace_count(user_id))) or 0
        )
    # SQLite remains the lightweight development/test backend.
    return int(
        db.scalar(
            select(func.count()).select_from(Membership).where(
                Membership.user_id == user_id,
                Membership.role == WorkspaceRole.OWNER,
            )
        )
        or 0
    )


@router.get("", response_model=WorkspaceList)
def list_workspaces(
    user: CurrentUserDep,
    db: DbDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> WorkspaceList:
    predicate = Membership.user_id == user.id
    total = db.scalar(select(func.count()).select_from(Membership).where(predicate)) or 0
    rows = db.execute(
        select(Workspace, Membership.role)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(predicate)
        .order_by(Workspace.name, Workspace.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return WorkspaceList(
        items=[_workspace_out(workspace, role) for workspace, role in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate,
    user: CurrentUserDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> WorkspaceOut:
    locked_user = db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None:
        raise ProblemException(
            401, "Nicht angemeldet", "Das Konto existiert nicht.", "invalid_session"
        )
    existing_count = _owned_workspace_count(db, user.id)
    if (
        not locked_user.is_platform_admin
        and existing_count >= settings.workspace_quota_per_user
    ):
        raise ProblemException(
            409,
            "Workspace-Limit erreicht",
            "Dein Konto hat bereits die maximal erlaubte Anzahl eigener Workspaces.",
            "workspace_quota_exceeded",
        )
    workspace = Workspace(name=payload.name.strip(), slug=_slug(payload.name))
    db.add(workspace)
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER
        )
    )
    db.commit()
    return _workspace_out(workspace, WorkspaceRole.OWNER)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(access: WorkspaceAccessDep) -> WorkspaceOut:
    return _workspace_out(access.workspace, access.role)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    payload: WorkspaceUpdate,
    access: WorkspaceAdminDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> WorkspaceOut:
    workspace = _lock_workspace(db, access.workspace.id)
    if payload.archived is True and workspace.archived_at is None:
        running_id = db.scalar(
            select(SyncRun.id)
            .where(
                SyncRun.workspace_id == workspace.id,
                SyncRun.status == SyncRunStatus.RUNNING,
            )
            .limit(1)
        )
        if running_id is not None:
            raise ProblemException(
                409,
                "Sync läuft noch",
                "Der Workspace kann erst archiviert werden, wenn der laufende Sync beendet ist.",
                "workspace_has_running_sync",
                headers={
                    "Location": (
                        f"{settings.api_prefix}/workspaces/{workspace.id}/runs/{running_id}"
                    )
                },
            )
    if payload.name is not None:
        workspace.name = payload.name.strip()
    if payload.archived is True and workspace.archived_at is None:
        now = datetime.now(timezone.utc)
        workspace.archived_at = now
        db.execute(
            update(SyncProfile)
            .where(SyncProfile.workspace_id == workspace.id)
            .values(enabled=False)
        )
        db.execute(
            update(SyncRun)
            .where(
                SyncRun.workspace_id == workspace.id,
                SyncRun.status == SyncRunStatus.QUEUED,
            )
            .values(
                status=SyncRunStatus.CANCELED,
                finished_at=now,
                error_json={
                    "code": "workspace_archived",
                    "message": "Der Workspace wurde archiviert.",
                },
            )
        )
    elif payload.archived is False:
        # Re-enabling profiles is an explicit administrator decision.
        workspace.archived_at = None
    db.commit()
    return _workspace_out(workspace, access.role)


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
def list_members(access: WorkspaceAccessDep, db: DbDep) -> list[MemberOut]:
    rows = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == access.workspace.id)
        .order_by(User.normalized_email)
    ).all()
    return [
        MemberOut(
            id=membership.id,
            user_id=user.id,
            email=user.email,
            role=membership.role,
            created_at=membership.created_at,
        )
        for membership, user in rows
    ]


@router.patch("/{workspace_id}/members/{membership_id}", response_model=MemberOut)
def update_member_role(
    membership_id: uuid.UUID,
    payload: MemberRoleUpdate,
    access: WorkspaceAdminDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> MemberOut:
    workspace = _lock_workspace(db, access.workspace.id)
    membership = db.scalar(
        select(Membership).where(
            Membership.id == membership_id,
            Membership.workspace_id == workspace.id,
        ).with_for_update()
    )
    if membership is None:
        raise ProblemException(404, "Nicht gefunden", "Mitglied nicht gefunden.", "not_found")
    if access.role != WorkspaceRole.OWNER and (
        membership.role == WorkspaceRole.OWNER or payload.role == WorkspaceRole.OWNER
    ):
        raise ProblemException(
            403,
            "Nicht berechtigt",
            "Nur Eigentümer dürfen die Eigentümerrolle ändern.",
            "owner_role_required",
        )
    if membership.role != WorkspaceRole.OWNER and payload.role == WorkspaceRole.OWNER:
        target_user = db.scalar(
            select(User)
            .where(User.id == membership.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if target_user is None:
            raise ProblemException(
                404, "Nicht gefunden", "Mitglied nicht gefunden.", "not_found"
            )
        owned_count = _owned_workspace_count(db, target_user.id)
        if (
            not target_user.is_platform_admin
            and owned_count >= settings.workspace_quota_per_user
        ):
            raise ProblemException(
                409,
                "Workspace-Limit erreicht",
                "Dieses Konto hat bereits die maximal erlaubte Anzahl eigener Workspaces.",
                "workspace_quota_exceeded",
            )
    if membership.role == WorkspaceRole.OWNER and payload.role != WorkspaceRole.OWNER:
        owner_count = db.scalar(
            select(func.count()).select_from(Membership).where(
                Membership.workspace_id == workspace.id,
                Membership.role == WorkspaceRole.OWNER,
            )
        )
        if owner_count == 1:
            raise ProblemException(
                409,
                "Letzter Eigentümer",
                "Ein Workspace benötigt mindestens einen Eigentümer.",
                "last_owner",
            )
    membership.role = payload.role
    db.commit()
    member = db.get(User, membership.user_id)
    assert member is not None
    return MemberOut(
        id=membership.id,
        user_id=member.id,
        email=member.email,
        role=membership.role,
        created_at=membership.created_at,
    )


@router.delete("/{workspace_id}/members/{membership_id}", status_code=204)
def remove_member(
    membership_id: uuid.UUID,
    access: WorkspaceAdminDep,
    db: DbDep,
    csrf: CsrfDep,
) -> None:
    workspace = _lock_workspace(db, access.workspace.id)
    membership = db.scalar(
        select(Membership).where(
            Membership.id == membership_id,
            Membership.workspace_id == workspace.id,
        ).with_for_update()
    )
    if membership is None:
        raise ProblemException(404, "Nicht gefunden", "Mitglied nicht gefunden.", "not_found")
    if membership.role == WorkspaceRole.OWNER:
        if access.role != WorkspaceRole.OWNER:
            raise ProblemException(403, "Nicht berechtigt", "Nur Eigentümer dürfen Eigentümer entfernen.", "owner_role_required")
        owner_count = db.scalar(
            select(func.count()).select_from(Membership).where(
                Membership.workspace_id == workspace.id,
                Membership.role == WorkspaceRole.OWNER,
            )
        ) or 0
        if owner_count <= 1:
            raise ProblemException(409, "Letzter Eigentümer", "Der letzte Eigentümer kann nicht entfernt werden.", "last_owner")
    db.delete(membership)
    db.commit()


@router.get("/{workspace_id}/invitations", response_model=list[InvitationOut])
def list_invitations(access: WorkspaceAdminDep, db: DbDep) -> list[WorkspaceInvitation]:
    return list(
        db.scalars(
            select(WorkspaceInvitation)
            .where(WorkspaceInvitation.workspace_id == access.workspace.id)
            .order_by(WorkspaceInvitation.created_at.desc())
        ).all()
    )


@router.post("/{workspace_id}/invitations", response_model=InvitationOut, status_code=201)
def invite_member(
    payload: InvitationCreate,
    access: WorkspaceAdminDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
    request: Request = None,
) -> InvitationOut:
    normalized = normalize_email(str(payload.email))
    enforce_auth_rate_limit(
        request,
        settings,
        "invite",
        identity=f"{access.workspace.id}:{normalized}",
    )
    # Existing invitations and acceptance use the same lock order:
    # invitation first, workspace second. New invitations have no row to lock
    # yet and are serialized by the workspace lock plus the unique constraint.
    invitation = db.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == access.workspace.id,
            WorkspaceInvitation.normalized_email == normalized,
        ).with_for_update()
    )
    workspace = _lock_workspace(db, access.workspace.id)
    if invitation is None:
        # A concurrent creator may have committed while this request waited on
        # the workspace lock. Re-read under the lock instead of relying on an
        # IntegrityError as normal control flow.
        invitation = db.scalar(
            select(WorkspaceInvitation).where(
                WorkspaceInvitation.workspace_id == workspace.id,
                WorkspaceInvitation.normalized_email == normalized,
            ).with_for_update()
        )
    existing_user_id = db.scalar(select(User.id).where(User.normalized_email == normalized))
    if existing_user_id and db.scalar(
        select(Membership.id).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == existing_user_id,
        )
    ):
        raise ProblemException(409, "Bereits Mitglied", "Diese Person ist bereits Mitglied.", "already_member")
    now = datetime.now(timezone.utc)
    occupied = db.scalar(
        select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace.id
        )
    ) or 0
    pending = db.scalar(
        select(func.count()).select_from(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == workspace.id,
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.expires_at > now,
        )
    ) or 0
    invitation_has_slot = (
        invitation is not None
        and invitation.accepted_at is None
        and _as_utc(invitation.expires_at) > now
    )
    required_slots = occupied + pending + (0 if invitation_has_slot else 1)
    if required_slots > workspace.member_quota:
        raise ProblemException(409, "Mitglieder-Limit erreicht", "Das Mitglieder-Limit dieses Workspaces ist erreicht.", "member_quota_exceeded")
    if invitation is not None:
        last_sent_at = db.scalar(
            select(NotificationOutbox.created_at)
            .where(
                NotificationOutbox.workspace_id == workspace.id,
                NotificationOutbox.channel == "email",
                NotificationOutbox.idempotency_key.like(
                    f"invitation:{invitation.id}:%"
                ),
            )
            .order_by(NotificationOutbox.created_at.desc())
            .limit(1)
        )
        if last_sent_at is not None:
            retry_at = _as_utc(last_sent_at) + timedelta(
                seconds=settings.invite_resend_cooldown_seconds
            )
            if retry_at > now:
                retry_after = max(1, int((retry_at - now).total_seconds()) + 1)
                raise ProblemException(
                    429,
                    "Einladung kürzlich versendet",
                    "Für diese Adresse wurde gerade bereits eine Einladung versendet.",
                    "invitation_resend_cooldown",
                    headers={"Retry-After": str(retry_after)},
                )
    raw_token = random_token()
    if invitation is None:
        invitation = WorkspaceInvitation(
            workspace_id=workspace.id,
            invited_by_user_id=access.user.id,
            email=str(payload.email),
            normalized_email=normalized,
            role=payload.role,
            token_hash=token_hash(settings, raw_token),
            expires_at=now + timedelta(days=7),
        )
        db.add(invitation)
    else:
        invitation.invited_by_user_id = access.user.id
        invitation.email = str(payload.email)
        invitation.role = payload.role
        invitation.token_hash = token_hash(settings, raw_token)
        invitation.expires_at = now + timedelta(days=7)
        invitation.accepted_at = None
    db.flush()
    accept_url = f"{settings.public_base_url.rstrip('/')}/invite?token={raw_token}"
    text_body, html_body = link_email_body(
        "Workspace-Einladung",
        f"Du wurdest zum Workspace „{workspace.name}“ eingeladen.",
        accept_url,
    )
    enqueue_email(
        db,
        settings,
        recipient=invitation.email,
        subject=f"Einladung zu {workspace.name}",
        text=text_body,
        html_body=html_body,
        workspace_id=workspace.id,
        idempotency_key=f"invitation:{invitation.id}:{invitation.token_hash[:16]}",
    )
    db.commit()
    return InvitationOut(
        id=invitation.id,
        workspace_id=invitation.workspace_id,
        email=invitation.email,
        role=invitation.role,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        development_invitation_token=(raw_token if settings.expose_development_tokens else None),
    )


@router.delete("/{workspace_id}/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    invitation_id: uuid.UUID,
    access: WorkspaceAdminDep,
    db: DbDep,
    csrf: CsrfDep,
) -> None:
    invitation = db.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.workspace_id == access.workspace.id,
        )
    )
    if invitation is None:
        raise ProblemException(404, "Nicht gefunden", "Einladung nicht gefunden.", "not_found")
    db.delete(invitation)
    db.commit()


@router.post("/invitations/accept", response_model=WorkspaceOut)
def accept_invitation(
    payload: InvitationAccept,
    user: CurrentUserDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> WorkspaceOut:
    raw_token_hash = token_hash(settings, payload.token.get_secret_value())
    invitation = db.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.token_hash == raw_token_hash,
            WorkspaceInvitation.accepted_at.is_(None),
        ).with_for_update()
    )
    now = datetime.now(timezone.utc)
    if invitation is None or _as_utc(invitation.expires_at) <= now:
        raise ProblemException(400, "Ungültige Einladung", "Die Einladung ist ungültig oder abgelaufen.", "invalid_invitation")
    workspace = _lock_workspace(db, invitation.workspace_id)
    if invitation.normalized_email != user.normalized_email:
        raise ProblemException(403, "Falsches Konto", "Diese Einladung wurde an eine andere E-Mail-Adresse gesendet.", "invitation_email_mismatch")
    if db.scalar(
        select(Membership.id).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == user.id,
        )
    ):
        raise ProblemException(
            409, "Bereits Mitglied", "Diese Person ist bereits Mitglied.", "already_member"
        )
    member_count = db.scalar(
        select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace.id
        )
    ) or 0
    if member_count >= workspace.member_quota:
        raise ProblemException(409, "Mitglieder-Limit erreicht", "Der Workspace kann keine weiteren Mitglieder aufnehmen.", "member_quota_exceeded")
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=invitation.role,
        )
    )
    # PostgreSQL's invitation UPDATE policy becomes true through the newly
    # inserted membership. Flush it first so WITH CHECK can observe it.
    db.flush()
    invitation.accepted_at = now
    db.commit()
    return _workspace_out(workspace, invitation.role)
