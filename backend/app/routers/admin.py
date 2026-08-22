from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from ..dependencies import AdminDbDep, CsrfDep, PlatformAdminDep
from ..models import AuditEvent, Membership, SyncProfile, Workspace
from ..problems import ProblemException
from ..schemas import AdminWorkspaceList, AdminWorkspaceOut, WorkspaceQuotaUpdate


router = APIRouter(prefix="/admin", tags=["Plattform-Administration"])


def _workspace_row(workspace: Workspace, members: int, profiles: int) -> AdminWorkspaceOut:
    return AdminWorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        archived_at=workspace.archived_at,
        profile_quota=workspace.profile_quota,
        member_quota=workspace.member_quota,
        profile_count=profiles,
        member_count=members,
        created_at=workspace.created_at,
    )


@router.get("/workspaces", response_model=AdminWorkspaceList)
def list_all_workspaces(
    admin: PlatformAdminDep,
    db: AdminDbDep,
    search: str | None = Query(default=None, min_length=1, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminWorkspaceList:
    predicates = []
    if search:
        pattern = f"%{search.strip()}%"
        predicates.append(or_(Workspace.name.ilike(pattern), Workspace.slug.ilike(pattern)))
    total = db.scalar(select(func.count()).select_from(Workspace).where(*predicates)) or 0
    member_count = (
        select(func.count())
        .select_from(Membership)
        .where(Membership.workspace_id == Workspace.id)
        .correlate(Workspace)
        .scalar_subquery()
    )
    profile_count = (
        select(func.count())
        .select_from(SyncProfile)
        .where(SyncProfile.workspace_id == Workspace.id)
        .correlate(Workspace)
        .scalar_subquery()
    )
    rows = db.execute(
        select(Workspace, member_count, profile_count)
        .where(*predicates)
        .order_by(Workspace.created_at.desc(), Workspace.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return AdminWorkspaceList(
        items=[_workspace_row(workspace, members, profiles) for workspace, members, profiles in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/workspaces/{workspace_id}/quotas", response_model=AdminWorkspaceOut)
def update_workspace_quotas(
    workspace_id: uuid.UUID,
    payload: WorkspaceQuotaUpdate,
    admin: PlatformAdminDep,
    db: AdminDbDep,
    csrf: CsrfDep,
) -> AdminWorkspaceOut:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ProblemException(404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found")
    previous = {
        "profile_quota": workspace.profile_quota,
        "member_quota": workspace.member_quota,
    }
    workspace.profile_quota = payload.profile_quota
    workspace.member_quota = payload.member_quota
    db.add(
        AuditEvent(
            workspace_id=workspace.id,
            actor_user_id=admin.id,
            action="workspace_quotas_updated",
            entity_type="workspace",
            entity_id=str(workspace.id),
            metadata_json={
                "previous": previous,
                "current": payload.model_dump(),
            },
        )
    )
    members = db.scalar(
        select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace.id
        )
    ) or 0
    profiles = db.scalar(
        select(func.count()).select_from(SyncProfile).where(
            SyncProfile.workspace_id == workspace.id
        )
    ) or 0
    db.commit()
    return _workspace_row(workspace, members, profiles)
