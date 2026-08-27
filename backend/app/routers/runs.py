from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import false, func, select
from sqlalchemy.orm import defer

from ..dependencies import CsrfDep, DbDep, SettingsDep, WorkspaceAccessDep, WorkspaceOperatorDep
from ..models import SyncAction, SyncProfile, SyncRun, SyncRunStatus, SyncTrigger, Workspace
from ..problems import ProblemException
from ..run_results import (
    action_ids_for_event,
    build_run_result,
    load_persisted_plan,
    preparation_action_ids,
)
from ..schemas import (
    RunCreate,
    SyncActionList,
    SyncActionStatusCounts,
    SyncRunDetail,
    SyncRunList,
    SyncRunOut,
    SyncRunResult,
)


router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Sync-Läufe"])
ACTIVE_STATUSES = (SyncRunStatus.QUEUED, SyncRunStatus.RUNNING)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _run_location(settings: SettingsDep, workspace_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{settings.api_prefix}/workspaces/{workspace_id}/runs/{run_id}"


@router.get("/runs", response_model=SyncRunList)
def list_runs(
    access: WorkspaceAccessDep,
    db: DbDep,
    profile_id: uuid.UUID | None = None,
    run_status: SyncRunStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SyncRunList:
    predicates = [SyncRun.workspace_id == access.workspace.id]
    if profile_id is not None:
        predicates.append(SyncRun.profile_id == profile_id)
    if run_status is not None:
        predicates.append(SyncRun.status == run_status)
    total = db.scalar(select(func.count()).select_from(SyncRun).where(*predicates)) or 0
    items = db.scalars(
        select(SyncRun)
        .options(defer(SyncRun.plan_json))
        .where(*predicates)
        .order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return SyncRunList(items=items, total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=SyncRunDetail)
def get_run(run_id: uuid.UUID, access: WorkspaceAccessDep, db: DbDep) -> SyncRun:
    run = db.scalar(
        select(SyncRun).where(
            SyncRun.id == run_id, SyncRun.workspace_id == access.workspace.id
        )
    )
    if run is None:
        raise ProblemException(404, "Nicht gefunden", "Sync-Lauf nicht gefunden.", "not_found")
    return run


@router.get("/runs/{run_id}/actions", response_model=SyncActionList)
def list_run_actions(
    run_id: uuid.UUID,
    access: WorkspaceAccessDep,
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2_147_483_647),
) -> SyncActionList:
    run_exists = db.scalar(
        select(SyncRun.id).where(
            SyncRun.id == run_id,
            SyncRun.workspace_id == access.workspace.id,
        )
    )
    if run_exists is None:
        raise ProblemException(
            404, "Nicht gefunden", "Sync-Lauf nicht gefunden.", "not_found"
        )
    grouped = db.execute(
        select(SyncAction.status, func.count())
        .where(SyncAction.run_id == run_id)
        .group_by(SyncAction.status)
    ).all()
    status_counts = {status.value: int(count) for status, count in grouped}
    total = sum(status_counts.values())
    items = db.scalars(
        select(SyncAction)
        .where(SyncAction.run_id == run_id)
        .order_by(SyncAction.ordinal, SyncAction.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return SyncActionList(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        status_counts=SyncActionStatusCounts(**status_counts),
    )


@router.get("/runs/{run_id}/result", response_model=SyncRunResult)
def get_run_result(
    run_id: uuid.UUID, access: WorkspaceAccessDep, db: DbDep
) -> SyncRunResult:
    run = _workspace_run(run_id, access, db)
    rows = db.scalars(
        select(SyncAction)
        .where(SyncAction.run_id == run.id)
        .order_by(SyncAction.ordinal, SyncAction.id)
    ).all()
    return build_run_result(run, rows)


@router.get(
    "/runs/{run_id}/result/events/{event_plan_id}/actions",
    response_model=SyncActionList,
)
def list_event_result_actions(
    run_id: uuid.UUID,
    event_plan_id: str,
    access: WorkspaceAccessDep,
    db: DbDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2_147_483_647),
) -> SyncActionList:
    run = _workspace_run(run_id, access, db)
    plan = load_persisted_plan(run)
    action_ids = action_ids_for_event(plan, event_plan_id) if plan is not None else None
    if action_ids is None:
        raise ProblemException(404, "Nicht gefunden", "Ereignisergebnis nicht gefunden.", "not_found")
    return _action_page(db, run.id, action_ids, limit, offset)


@router.get(
    "/runs/{run_id}/result/preparation-actions",
    response_model=SyncActionList,
)
def list_preparation_actions(
    run_id: uuid.UUID,
    access: WorkspaceAccessDep,
    db: DbDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2_147_483_647),
) -> SyncActionList:
    run = _workspace_run(run_id, access, db)
    plan = load_persisted_plan(run)
    ids = preparation_action_ids(plan) if plan is not None else ()
    return _action_page(db, run.id, ids, limit, offset)


def _workspace_run(run_id: uuid.UUID, access: WorkspaceAccessDep, db: DbDep) -> SyncRun:
    run = db.scalar(
        select(SyncRun).where(
            SyncRun.id == run_id, SyncRun.workspace_id == access.workspace.id
        )
    )
    if run is None:
        raise ProblemException(404, "Nicht gefunden", "Sync-Lauf nicht gefunden.", "not_found")
    return run


def _action_page(
    db: DbDep,
    run_id: uuid.UUID,
    action_ids: tuple[uuid.UUID, ...],
    limit: int,
    offset: int,
) -> SyncActionList:
    predicate = SyncAction.id.in_(action_ids) if action_ids else false()
    grouped = db.execute(
        select(SyncAction.status, func.count())
        .where(SyncAction.run_id == run_id, predicate)
        .group_by(SyncAction.status)
    ).all()
    status_counts = {status.value: int(count) for status, count in grouped}
    total = sum(status_counts.values())
    items = db.scalars(
        select(SyncAction)
        .where(SyncAction.run_id == run_id, predicate)
        .order_by(SyncAction.ordinal, SyncAction.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return SyncActionList(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        status_counts=SyncActionStatusCounts(**status_counts),
    )


@router.post("/profiles/{profile_id}/runs", response_model=SyncRunOut, status_code=202)
def start_run(
    profile_id: uuid.UUID,
    payload: RunCreate,
    request: Request,
    access: WorkspaceOperatorDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> SyncRun:
    # Workspace archival uses the same lock order. Locking workspace before
    # profile closes the start/archive race without a deadlock cycle.
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == access.workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ProblemException(
            404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found"
        )
    if workspace.archived_at is not None:
        raise ProblemException(
            409,
            "Workspace archiviert",
            "In einem archivierten Workspace können keine Sync-Läufe gestartet werden.",
            "workspace_archived",
        )
    profile = db.scalar(
        select(SyncProfile)
        .where(
            SyncProfile.id == profile_id,
            SyncProfile.workspace_id == access.workspace.id,
        )
        .with_for_update()
    )
    if profile is None:
        raise ProblemException(404, "Nicht gefunden", "Sync-Profil nicht gefunden.", "not_found")
    active = db.scalar(
        select(SyncRun).where(
            SyncRun.profile_id == profile.id,
            SyncRun.status.in_(ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise ProblemException(
            409,
            "Sync läuft bereits",
            f"Für dieses Profil läuft bereits der Sync {active.id}.",
            "run_already_active",
            headers={
                "Location": _run_location(
                    settings, access.workspace.id, active.id
                )
            },
        )
    now = datetime.now(timezone.utc)
    last_manual = None
    if not payload.dry_run:
        last_manual = db.scalar(
            select(SyncRun)
            .where(
                SyncRun.profile_id == profile.id,
                SyncRun.trigger == SyncTrigger.MANUAL,
                SyncRun.dry_run.is_(False),
            )
            .order_by(SyncRun.created_at.desc())
            .limit(1)
        )
    if last_manual is not None:
        retry_at = _as_utc(last_manual.created_at) + timedelta(
            seconds=settings.manual_run_cooldown_seconds
        )
        if retry_at > now:
            retry_after = max(1, int((retry_at - now).total_seconds()))
            raise ProblemException(
                429,
                "Manueller Start zu früh",
                "Zwischen manuellen Starts müssen mindestens 30 Minuten liegen.",
                "manual_run_cooldown",
                headers={"Retry-After": str(retry_after)},
            )
    run = SyncRun(
        workspace_id=access.workspace.id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=SyncRunStatus.QUEUED,
        trigger=SyncTrigger.MANUAL,
        dry_run=payload.dry_run,
        # Persist the attempt together with the run. A process crash before or
        # during broker send is retried after the scheduler backoff without a
        # 15-second duplicate storm.
        dispatch_attempted_at=now,
    )
    db.add(run)
    db.commit()
    try:
        request.app.state.run_dispatcher.enqueue(run.id)
    except Exception:
        # The row was committed before the broker send. Keep it recoverable:
        # the scheduler deliberately redelivers unsent durable queued runs,
        # closing the database/broker dual-write gap.
        raise ProblemException(
            503,
            "Queue nicht verfügbar",
            "Der Lauf wurde gespeichert, konnte aber nicht eingereiht werden.",
            "queue_unavailable",
            headers={
                "Location": _run_location(
                    settings, access.workspace.id, run.id
                )
            },
        )
    return run


@router.post("/profiles/{profile_id}/preview", response_model=SyncRunOut, status_code=202)
def preview_run(
    profile_id: uuid.UUID,
    request: Request,
    access: WorkspaceOperatorDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> SyncRun:
    return start_run(
        profile_id=profile_id,
        payload=RunCreate(dry_run=True),
        request=request,
        access=access,
        settings=settings,
        db=db,
        csrf=csrf,
    )


@router.post("/runs/{run_id}/cancel", response_model=SyncRunOut)
def cancel_run(
    run_id: uuid.UUID,
    access: WorkspaceOperatorDep,
    db: DbDep,
    csrf: CsrfDep,
) -> SyncRun:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == access.workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ProblemException(
            404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found"
        )
    run = db.scalar(
        select(SyncRun).where(
            SyncRun.id == run_id,
            SyncRun.workspace_id == workspace.id,
        ).with_for_update()
    )
    if run is None:
        raise ProblemException(404, "Nicht gefunden", "Sync-Lauf nicht gefunden.", "not_found")
    if run.status != SyncRunStatus.QUEUED:
        raise ProblemException(409, "Lauf nicht abbrechbar", "Nur eingereihte Läufe können direkt abgebrochen werden.", "run_not_cancelable")
    run.status = SyncRunStatus.CANCELED
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    return run
