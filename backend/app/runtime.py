"""Production adapters connecting the sync domain to SQLAlchemy and Redis."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterator, Mapping, Sequence
from redis import Redis
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import Database
from .dramatiq_setup import configure_dramatiq
from .event_filters import canonicalize_persisted_event_rules
from .models import (
    ProviderConnection,
    ProviderType,
    NotificationOutbox,
    OutboxStatus,
    RemoteBinding,
    SyncAction,
    SyncActionKind,
    SyncActionStatus,
    SyncProfile,
    SyncRun,
    SyncRunStatus,
    SyncTrigger,
    Workspace,
)
from .outbox import set_worker_context
from .providers import ChurchToolsClient, WorshipToolsClient
from .security import SecretCipher
from .scheduling import next_schedule_after
from .sync.errors import AuthenticationError, ConcurrentModificationError, SchemaDriftError
from .sync.leases import RedisEventLeaseManager
from .sync.models import (
    ActionExecution,
    ActionKind,
    ActionStatus,
    AgendaAnchor,
    AnchorRelation,
    EventSelector,
    MatchMode,
    Ownership,
    PlacementRule,
    PlannedAction,
    ProfileConfig,
    RunSpecification,
    RunStatus,
    SyncPlan,
)
from .sync.serialization import sync_plan_from_dict

logger = logging.getLogger(__name__)

_ACTIVE_RUN_STATUSES = (SyncRunStatus.QUEUED, SyncRunStatus.RUNNING)


@contextmanager
def _worker_session(database: Database) -> Iterator[Session]:
    """Every background transaction explicitly enters the worker RLS scope."""

    db = database.session_factory()
    try:
        set_worker_context(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class SqlRunRepository:
    """Thread-safe, short-session implementation for Dramatiq workers."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def claim(
        self, run_id: str, worker_id: str, lease_seconds: int
    ) -> RunSpecification | None:
        return await asyncio.to_thread(
            self._claim, _uuid(run_id), worker_id, lease_seconds
        )

    def _claim(
        self, run_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> RunSpecification | None:
        now = _utcnow()
        with _worker_session(self.database) as db:
            workspace_id = db.scalar(
                select(SyncRun.workspace_id).where(SyncRun.id == run_id)
            )
            if workspace_id is None:
                return None
            workspace_query = select(Workspace).where(Workspace.id == workspace_id)
            if db.get_bind().dialect.name == "postgresql":
                workspace_query = workspace_query.with_for_update()
            workspace = db.scalar(workspace_query)
            if workspace is None or workspace.archived_at is not None:
                db.execute(
                    update(SyncRun)
                    .where(
                        SyncRun.id == run_id,
                        SyncRun.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                    .values(
                        status=SyncRunStatus.CANCELED,
                        finished_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                        error_json={
                            "kind": "workspace_archived",
                            "message": "Workspace ist archiviert; Lauf abgebrochen.",
                            "retryable": False,
                        },
                    )
                    .execution_options(synchronize_session=False)
                )
                return None
            claimable = or_(
                SyncRun.status == SyncRunStatus.QUEUED,
                (
                    (SyncRun.status == SyncRunStatus.RUNNING)
                    & (SyncRun.lease_expires_at.is_not(None))
                    & (SyncRun.lease_expires_at < now)
                ),
            )
            result = db.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id, claimable)
                .values(
                    status=SyncRunStatus.RUNNING,
                    started_at=func.coalesce(SyncRun.started_at, now),
                    lease_owner=worker_id[:200],
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return None
            run = db.get(SyncRun, run_id)
            if run is None:
                return None
            profile = db.get(SyncProfile, run.profile_id)
            if profile is None:
                run.status = SyncRunStatus.FAILED
                run.finished_at = now
                run.error_json = {
                    "kind": "configuration",
                    "message": "Sync-Profil wurde vor Ausführung entfernt",
                    "retryable": False,
                }
                run.lease_owner = None
                run.lease_expires_at = None
                return None
            if run.plan_json is None and profile.revision != run.config_revision:
                run.status = SyncRunStatus.SKIPPED
                run.finished_at = now
                run.error_json = {
                    "kind": "configuration_revision_changed",
                    "message": "Profil wurde nach dem Einreihen geändert; Lauf übersprungen",
                    "retryable": False,
                }
                run.lease_owner = None
                run.lease_expires_at = None
                return None
            try:
                config = _profile_config(
                    profile,
                    revision=(run.config_revision if run.plan_json is not None else profile.revision),
                )
            except (TypeError, ValueError) as exc:
                run.status = SyncRunStatus.FAILED
                run.finished_at = now
                run.error_json = {
                    "kind": "invalid_profile_configuration",
                    "message": "Sync-Profil enthält eine ungültige Konfiguration",
                    "retryable": False,
                }
                run.lease_owner = None
                run.lease_expires_at = None
                logger.warning(
                    "invalid profile configuration",
                    extra={
                        "profile_id": str(profile.id),
                        "error_type": type(exc).__name__,
                    },
                )
                return None
            return RunSpecification(
                run_id=str(run.id),
                workspace_id=str(run.workspace_id),
                source_connection_id=str(profile.source_connection_id),
                target_connection_id=str(profile.target_connection_id),
                profile=config,
                dry_run=run.dry_run,
            )

    async def renew_lease(
        self, run_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        return await asyncio.to_thread(
            self._renew_lease, _uuid(run_id), worker_id, lease_seconds
        )

    def _renew_lease(
        self, run_id: uuid.UUID, worker_id: str, lease_seconds: int
    ) -> bool:
        with _worker_session(self.database) as db:
            result = db.execute(
                update(SyncRun)
                .where(
                    SyncRun.id == run_id,
                    SyncRun.status == SyncRunStatus.RUNNING,
                    SyncRun.lease_owner == worker_id[:200],
                    SyncRun.lease_expires_at.is_not(None),
                    SyncRun.lease_expires_at > _utcnow(),
                )
                .values(lease_expires_at=_utcnow() + timedelta(seconds=lease_seconds))
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1

    async def ownerships(
        self, profile_id: str, target_event_id: str
    ) -> Sequence[Ownership]:
        return await asyncio.to_thread(
            self._ownerships, _uuid(profile_id), target_event_id
        )

    def _ownerships(
        self, profile_id: uuid.UUID, target_event_id: str
    ) -> tuple[Ownership, ...]:
        with _worker_session(self.database) as db:
            profile = db.get(SyncProfile, profile_id)
            if profile is None:
                return ()
            # Include other profiles on the same connection/event so the pure
            # planner can reject sequential ownership collisions.
            rows = db.scalars(
                select(RemoteBinding).where(
                    RemoteBinding.target_connection_id == profile.target_connection_id,
                    RemoteBinding.target_event_id == target_event_id,
                )
            ).all()
            return tuple(
                Ownership(
                    profile_id=str(row.profile_id),
                    target_event_id=row.target_event_id,
                    agenda_item_id=row.agenda_item_id,
                    source_key=row.source_key,
                    placement_id=row.placement_id,
                    fingerprint=dict(row.fingerprint_json or {}),
                )
                for row in rows
            )

    async def load_plan(self, run_id: str) -> SyncPlan | None:
        return await asyncio.to_thread(self._load_plan, _uuid(run_id))

    def _load_plan(self, run_id: uuid.UUID) -> SyncPlan | None:
        with _worker_session(self.database) as db:
            run = db.get(SyncRun, run_id)
            if run is None or run.plan_json is None:
                return None
            document = run.plan_json
            raw_plan = document.get("plan") if "plan" in document else document
            if not isinstance(raw_plan, Mapping):
                raise SchemaDriftError("Persisted sync plan wrapper is invalid")
            plan = sync_plan_from_dict(raw_plan)
            expected = document.get("fingerprint")
            if expected is not None and expected != plan.fingerprint:
                raise ConcurrentModificationError("Persisted sync plan fingerprint mismatch")
            return plan

    async def persist_plan(
        self, run_id: str, plan: SyncPlan, owner_token: str
    ) -> None:
        await asyncio.to_thread(
            self._persist_plan, _uuid(run_id), plan, owner_token
        )

    def _persist_plan(
        self, run_id: uuid.UUID, plan: SyncPlan, owner_token: str
    ) -> None:
        with _worker_session(self.database) as db:
            run = _active_leased_run(db, run_id, owner_token)
            if run.plan_json is not None:
                raw = run.plan_json.get("plan", run.plan_json)
                existing = sync_plan_from_dict(raw)
                if existing.fingerprint != plan.fingerprint:
                    raise ConcurrentModificationError("Run already contains a different plan")
                return
            run.plan_json = {
                "schema_version": 1,
                "fingerprint": plan.fingerprint,
                "plan": plan.as_dict(),
            }
            run.planned_at = _utcnow()
            event_by_plan_id = {event.id: event for event in plan.events}
            actions = list(plan.preparation_actions)
            actions.extend(action for event in plan.events for action in event.actions)
            for action in actions:
                event = event_by_plan_id.get(action.event_plan_id or "")
                db.add(
                    SyncAction(
                        id=_uuid(action.id),
                        run_id=run.id,
                        event_id=event.target_event_id if event else None,
                        source_id=str(
                            action.payload.get("source_song_id")
                            or (event.source_event_id if event else action.payload.get("source_key") or "")
                        )[:200]
                        or None,
                        target_id=_planned_target_id(action),
                        kind=SyncActionKind(action.kind.value),
                        status=SyncActionStatus.PLANNED,
                        ordinal=action.ordinal,
                        payload_json=dict(action.payload),
                    )
                )

    async def prior_executions(
        self, run_id: str
    ) -> Mapping[str, ActionExecution]:
        return await asyncio.to_thread(self._prior_executions, _uuid(run_id))

    def _prior_executions(
        self, run_id: uuid.UUID
    ) -> dict[str, ActionExecution]:
        with _worker_session(self.database) as db:
            rows = db.scalars(
                select(SyncAction).where(SyncAction.run_id == run_id)
            ).all()
            return {
                row.id.hex: ActionExecution(
                    action_id=row.id.hex,
                    status=ActionStatus(row.status.value),
                    result=dict(row.fingerprint_json or {}),
                    error=dict(row.error_json) if row.error_json else None,
                )
                for row in rows
            }

    async def action_started(
        self, run_id: str, action: PlannedAction, owner_token: str
    ) -> None:
        await asyncio.to_thread(
            self._action_started, _uuid(run_id), action, owner_token
        )

    def _action_started(
        self, run_id: uuid.UUID, action: PlannedAction, owner_token: str
    ) -> None:
        with _worker_session(self.database) as db:
            _active_leased_run(db, run_id, owner_token)
            row = db.get(SyncAction, _uuid(action.id))
            if row is None or row.run_id != run_id:
                raise ConcurrentModificationError("Planned action is missing")
            if row.status != SyncActionStatus.PLANNED:
                raise ConcurrentModificationError("Planned action already changed state")

    async def action_finished(
        self, run_id: str, execution: ActionExecution, owner_token: str
    ) -> None:
        await asyncio.to_thread(
            self._action_finished, _uuid(run_id), execution, owner_token
        )

    def _action_finished(
        self,
        run_id: uuid.UUID,
        execution: ActionExecution,
        owner_token: str,
    ) -> None:
        now = _utcnow()
        with _worker_session(self.database) as db:
            _active_leased_run(db, run_id, owner_token)
            row = db.get(SyncAction, _uuid(execution.action_id))
            if row is None or row.run_id != run_id:
                raise ConcurrentModificationError("Sync action is missing")
            if row.status in {
                SyncActionStatus.VERIFIED,
                SyncActionStatus.FAILED,
                SyncActionStatus.SKIPPED,
            }:
                # Terminal action outcomes never regress on duplicate delivery.
                return
            if (
                row.status == SyncActionStatus.APPLIED
                and execution.status in {ActionStatus.PLANNED, ActionStatus.SKIPPED}
            ):
                return
            row.status = SyncActionStatus(execution.status.value)
            row.fingerprint_json = dict(execution.result) if execution.result else None
            row.error_json = dict(execution.error) if execution.error else None
            if execution.status is ActionStatus.APPLIED:
                row.applied_at = now
            elif execution.status is ActionStatus.VERIFIED:
                row.applied_at = row.applied_at or now
                row.verified_at = now
            target_id = (
                execution.result.get("agenda_item_id")
                or execution.result.get("arrangement_id")
                or execution.result.get("song_id")
            )
            if target_id is not None:
                row.target_id = str(target_id)[:200]

    async def bind_ownership(
        self, run_id: str, ownership: Ownership, owner_token: str
    ) -> None:
        await asyncio.to_thread(
            self._bind_ownership, _uuid(run_id), ownership, owner_token
        )

    def _bind_ownership(
        self, run_id: uuid.UUID, ownership: Ownership, owner_token: str
    ) -> None:
        profile_id = _uuid(ownership.profile_id)
        with _worker_session(self.database) as db:
            _active_leased_run(db, run_id, owner_token)
            profile = db.get(SyncProfile, profile_id)
            if profile is None:
                raise ConcurrentModificationError("Profile disappeared while binding ownership")
            existing = db.scalar(
                select(RemoteBinding)
                .where(
                    RemoteBinding.target_connection_id == profile.target_connection_id,
                    RemoteBinding.target_event_id == ownership.target_event_id,
                    RemoteBinding.agenda_item_id == ownership.agenda_item_id,
                )
                .with_for_update()
            )
            if existing is None:
                existing = RemoteBinding(
                    workspace_id=profile.workspace_id,
                    profile_id=profile.id,
                    target_connection_id=profile.target_connection_id,
                    target_event_id=ownership.target_event_id,
                    agenda_item_id=ownership.agenda_item_id,
                    source_key=ownership.source_key,
                    placement_id=ownership.placement_id,
                )
                db.add(existing)
            else:
                if existing.profile_id != profile.id:
                    raise ConcurrentModificationError(
                        "Agenda item is already owned by another sync profile",
                        target_event_id=ownership.target_event_id,
                        agenda_item_id=ownership.agenda_item_id,
                    )
                existing.workspace_id = profile.workspace_id
                existing.source_key = ownership.source_key
                existing.placement_id = ownership.placement_id
            existing.fingerprint_json = dict(ownership.fingerprint or {}) or None

    async def unbind_ownership(
        self,
        run_id: str,
        profile_id: str,
        target_event_id: str,
        agenda_item_id: str,
        owner_token: str,
    ) -> None:
        await asyncio.to_thread(
            self._unbind_ownership,
            _uuid(run_id),
            _uuid(profile_id),
            target_event_id,
            agenda_item_id,
            owner_token,
        )

    def _unbind_ownership(
        self,
        run_id: uuid.UUID,
        profile_id: uuid.UUID,
        target_event_id: str,
        agenda_item_id: str,
        owner_token: str,
    ) -> None:
        with _worker_session(self.database) as db:
            _active_leased_run(db, run_id, owner_token)
            row = db.scalar(
                select(RemoteBinding).where(
                    RemoteBinding.profile_id == profile_id,
                    RemoteBinding.target_event_id == target_event_id,
                    RemoteBinding.agenda_item_id == agenda_item_id,
                )
            )
            if row is not None:
                db.delete(row)

    async def cancel_requested(self, run_id: str) -> bool:
        return await asyncio.to_thread(self._cancel_requested, _uuid(run_id))

    def _cancel_requested(self, run_id: uuid.UUID) -> bool:
        with _worker_session(self.database) as db:
            status = db.scalar(select(SyncRun.status).where(SyncRun.id == run_id))
            return status == SyncRunStatus.CANCELED

    async def finish(
        self,
        run_id: str,
        status: RunStatus,
        error: Mapping[str, Any] | None = None,
        *,
        worker_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._finish, _uuid(run_id), status, error, worker_id
        )

    def _finish(
        self,
        run_id: uuid.UUID,
        status: RunStatus,
        error: Mapping[str, Any] | None,
        worker_id: str | None,
    ) -> bool:
        with _worker_session(self.database) as db:
            if worker_id is None:
                return False
            try:
                run = _active_leased_run(db, run_id, worker_id)
            except ConcurrentModificationError:
                logger.warning(
                    "ignored stale completion for sync run %s from worker %s",
                    run_id,
                    worker_id,
                )
                return False
            run.status = SyncRunStatus(status.value)
            run.error_json = dict(error) if error else None
            run.finished_at = _utcnow()
            run.lease_owner = None
            run.lease_expires_at = None
            return True


class SqlDueRunRepository:
    def __init__(self, database: Database, *, redelivery_seconds: int = 300) -> None:
        self.database = database
        if redelivery_seconds < 30:
            raise ValueError("dispatch redelivery interval must be at least 30 seconds")
        self.redelivery_seconds = redelivery_seconds

    async def mark_dispatched(
        self, run_id: str, dispatched_at: datetime
    ) -> bool:
        return await asyncio.to_thread(
            self._mark_dispatched, _uuid(run_id), _as_utc(dispatched_at)
        )

    def _mark_dispatched(
        self, run_id: uuid.UUID, dispatched_at: datetime
    ) -> bool:
        with _worker_session(self.database) as db:
            result = db.execute(
                update(SyncRun)
                .where(
                    SyncRun.id == run_id,
                    SyncRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
                .values(dispatch_attempted_at=dispatched_at, error_json=None)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1

    async def notification_work_due(self, now: datetime) -> bool:
        return await asyncio.to_thread(self._notification_work_due, _as_utc(now))

    def _notification_work_due(self, now: datetime) -> bool:
        with _worker_session(self.database) as db:
            unfanned_run = db.scalar(
                select(SyncRun.id)
                .where(
                    SyncRun.status.not_in(_ACTIVE_RUN_STATUSES),
                    SyncRun.notifications_fanned_out_at.is_(None),
                )
                .limit(1)
            )
            if unfanned_run is not None:
                return True
            due_outbox = db.scalar(
                select(NotificationOutbox.id)
                .where(
                    or_(
                        and_(
                            NotificationOutbox.status.in_(
                                (OutboxStatus.PENDING, OutboxStatus.FAILED)
                            ),
                            NotificationOutbox.next_attempt_at <= now,
                        ),
                        and_(
                            NotificationOutbox.status == OutboxStatus.PROCESSING,
                            NotificationOutbox.next_attempt_at <= now,
                        ),
                    )
                )
                .limit(1)
            )
            return due_outbox is not None

    async def create_due_runs(
        self, now: datetime, limit: int
    ) -> Sequence[str]:
        return await asyncio.to_thread(self._create_due_runs, now, limit)

    def _create_due_runs(self, now: datetime, limit: int) -> tuple[str, ...]:
        now = _as_utc(now)
        batch_limit = max(1, min(limit, 1000))
        retry_before = now - timedelta(seconds=self.redelivery_seconds)
        queued: list[str] = []
        with _worker_session(self.database) as db:
            # Transactional run creation and broker send cannot be atomic. Any
            # unsent run (commit-before-send crash) and any expired running
            # lease is redelivered. Successful sends are throttled so a queue
            # backlog cannot accumulate duplicate messages every scheduler tick.
            recoverable_query = (
                select(SyncRun)
                .join(Workspace, Workspace.id == SyncRun.workspace_id)
                .where(
                    Workspace.archived_at.is_(None),
                    or_(
                        (
                            (SyncRun.status == SyncRunStatus.QUEUED)
                            & or_(
                                SyncRun.dispatch_attempted_at.is_(None),
                                SyncRun.dispatch_attempted_at <= retry_before,
                            )
                        ),
                        (
                            (SyncRun.status == SyncRunStatus.RUNNING)
                            & (SyncRun.lease_expires_at.is_not(None))
                            & (SyncRun.lease_expires_at < now)
                        ),
                    )
                )
                .order_by(SyncRun.created_at, SyncRun.id)
                .limit(batch_limit)
            )
            if db.get_bind().dialect.name == "postgresql":
                recoverable_query = recoverable_query.with_for_update(
                    of=SyncRun, skip_locked=True
                )
            recoverable = db.scalars(recoverable_query).all()
            # Claim the attempt transactionally before broker I/O. A
            # commit-before-send crash is retried after the bounded timeout,
            # rather than producing a new message every scheduler tick.
            for run in recoverable:
                if run.status == SyncRunStatus.RUNNING:
                    # Expired executions are immediately fenced and returned
                    # to QUEUED. This attempt marker then prevents repeated
                    # delivery on every scheduler tick.
                    run.status = SyncRunStatus.QUEUED
                    run.lease_owner = None
                    run.lease_expires_at = None
                run.dispatch_attempted_at = now
            queued.extend(str(run.id) for run in recoverable)
            remaining = batch_limit - len(queued)
            if remaining <= 0:
                return tuple(queued)
            query = (
                select(SyncProfile)
                .join(Workspace, Workspace.id == SyncProfile.workspace_id)
                .where(
                    Workspace.archived_at.is_(None),
                    SyncProfile.enabled.is_(True),
                    or_(
                        SyncProfile.next_scheduled_at.is_(None),
                        SyncProfile.next_scheduled_at <= now,
                    ),
                    ~exists(
                        select(SyncRun.id).where(
                            SyncRun.profile_id == SyncProfile.id,
                            SyncRun.status.in_(_ACTIVE_RUN_STATUSES),
                        )
                    ),
                )
                .order_by(
                    SyncProfile.next_scheduled_at.asc().nullsfirst(),
                    SyncProfile.id,
                )
                .limit(remaining)
            )
            if db.get_bind().dialect.name == "postgresql":
                query = query.with_for_update(
                    of=(Workspace, SyncProfile), skip_locked=True
                )
            profiles = db.scalars(query).all()
            for profile in profiles:
                try:
                    following = next_schedule_after(
                        schedule_type=profile.schedule_type,
                        interval_minutes=profile.interval_minutes,
                        cron_expression=profile.cron_expression,
                        timezone_name=profile.target_timezone,
                        after=now,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    # Persist the configuration failure in history and move
                    # the retry horizon forward so one corrupt profile cannot
                    # hot-loop or block healthy tenants.
                    logger.error(
                        "invalid profile schedule",
                        extra={
                            "profile_id": str(profile.id),
                            "error_type": type(exc).__name__,
                        },
                    )
                    db.add(
                        SyncRun(
                            workspace_id=profile.workspace_id,
                            profile_id=profile.id,
                            config_revision=profile.revision,
                            status=SyncRunStatus.FAILED,
                            trigger=SyncTrigger.SCHEDULED,
                            dry_run=False,
                            finished_at=now,
                            error_json={
                                "kind": "invalid_schedule",
                                "message": "Der Profil-Zeitplan ist ungültig.",
                                "retryable": False,
                            },
                        )
                    )
                    profile.next_scheduled_at = now + timedelta(days=1)
                    continue
                run = SyncRun(
                    workspace_id=profile.workspace_id,
                    profile_id=profile.id,
                    config_revision=profile.revision,
                    status=SyncRunStatus.QUEUED,
                    trigger=SyncTrigger.SCHEDULED,
                    dry_run=False,
                    dispatch_attempted_at=now,
                )
                db.add(run)
                db.flush()
                profile.last_scheduled_at = now
                profile.next_scheduled_at = following
                queued.append(str(run.id))
        return tuple(queued)


class DatabaseProviderRegistry:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.cipher = SecretCipher(settings)

    def source(
        self, workspace_id: str, connection_id: str, source_timezone: str
    ) -> WorshipToolsClient:
        connection, credentials = self._connection(
            workspace_id, connection_id, ProviderType.WORSHIPTOOLS
        )
        email = _required_secret(credentials, "email", "WorshipTools email")
        password = _required_secret(
            credentials, "password", "WorshipTools password", strip=False
        )
        account_id = str(credentials.get("account_id") or "").strip()
        if not account_id:
            raise AuthenticationError("WorshipTools account_id is not configured")
        return WorshipToolsClient(email, password, account_id, source_timezone)

    def target(
        self, workspace_id: str, connection_id: str, target_timezone: str
    ) -> ChurchToolsClient:
        connection, credentials = self._connection(
            workspace_id, connection_id, ProviderType.CHURCHTOOLS
        )
        token = _churchtools_token(credentials)
        if not connection.base_url:
            raise AuthenticationError("ChurchTools base URL is not configured")
        return ChurchToolsClient(connection.base_url, token, target_timezone)

    def _connection(
        self, workspace_id: str, connection_id: str, provider: ProviderType
    ) -> tuple[ProviderConnection, dict[str, Any]]:
        with _worker_session(self.database) as db:
            connection = db.scalar(
                select(ProviderConnection).where(
                    ProviderConnection.id == _uuid(connection_id),
                    ProviderConnection.workspace_id == _uuid(workspace_id),
                    ProviderConnection.provider == provider,
                )
            )
            if connection is None:
                raise AuthenticationError("Provider connection not found in run workspace")
            # Copy the detached row's JSON before closing the session.
            settings_json = dict(connection.settings_json or {})
            encrypted = connection.credentials_encrypted
            detached = ProviderConnection(
                id=connection.id,
                workspace_id=connection.workspace_id,
                provider=connection.provider,
                name=connection.name,
                base_url=connection.base_url,
                settings_json=settings_json,
            )
        if not encrypted:
            raise AuthenticationError("Provider credentials are not configured")
        try:
            credentials = self.cipher.decrypt_json(
                encrypted, context=f"connection:{detached.id}"
            )
        except (TypeError, ValueError) as exc:
            raise AuthenticationError(
                "Provider credentials could not be decrypted"
            ) from exc
        return detached, credentials


class ProviderConnectionTester:
    """Synchronous FastAPI adapter around the async provider clients."""

    def test(
        self, connection: ProviderConnection, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        configuration_error = _connection_configuration_error(
            connection, credentials
        )
        if configuration_error is not None:
            return {
                "succeeded": False,
                "message": configuration_error,
                "identity": {},
                "capabilities": [],
            }
        try:
            return asyncio.run(self._test(connection, credentials))
        except AuthenticationError:
            provider_name = (
                "ChurchTools"
                if connection.provider == ProviderType.CHURCHTOOLS
                else "WorshipTools"
            )
            return {
                "succeeded": False,
                "message": f"{provider_name}-Authentifizierung fehlgeschlagen.",
                "identity": {},
                "capabilities": [],
            }

    async def _test(
        self, connection: ProviderConnection, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        client = _client_from_connection(connection, credentials)
        try:
            if isinstance(client, ChurchToolsClient):
                identity = dict(await client.validate())
                return {
                    "succeeded": True,
                    "message": "ChurchTools-Verbindung erfolgreich.",
                    "identity": {key: identity[key] for key in ("id", "email") if key in identity},
                    "capabilities": ["events", "agenda", "songs", "metadata"],
                }
            await client.validate()
            return {
                "succeeded": True,
                "message": "WorshipTools-Verbindung erfolgreich.",
                "identity": {
                    "email": _required_secret(credentials, "email", "WorshipTools email"),
                    "account_id": _account_id(connection, credentials),
                },
                "capabilities": ["services", "songs"],
            }
        finally:
            await client.aclose()

    def metadata(
        self, connection: ProviderConnection, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        return asyncio.run(self._metadata(connection, credentials))

    async def _metadata(
        self, connection: ProviderConnection, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        client = _client_from_connection(connection, credentials)
        try:
            if isinstance(client, ChurchToolsClient):
                return await client.metadata()
            await client.validate()
            return {
                "calendars": [],
                "campuses": [],
                "song_categories": [],
            }
        finally:
            await client.aclose()


class ApiRunDispatcher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        configure_dramatiq(self.settings)

    def enqueue(self, run_id: uuid.UUID) -> None:
        from .worker import sync_run_actor

        if sync_run_actor is None:
            raise RuntimeError("Dramatiq is unavailable")
        sync_run_actor.send(str(run_id))


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    settings: Settings
    database: Database
    run_repository: SqlRunRepository
    due_repository: SqlDueRunRepository
    providers: DatabaseProviderRegistry
    event_leases: RedisEventLeaseManager


@lru_cache(maxsize=1)
def runtime_context() -> RuntimeContext:
    settings = get_settings()
    database = Database(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return RuntimeContext(
        settings=settings,
        database=database,
        run_repository=SqlRunRepository(database),
        due_repository=SqlDueRunRepository(
            database,
            redelivery_seconds=settings.scheduler_redelivery_seconds,
        ),
        providers=DatabaseProviderRegistry(database, settings),
        event_leases=RedisEventLeaseManager(redis),
    )


def worker_dependencies():
    from .worker import WorkerDependencies

    context = runtime_context()
    return WorkerDependencies(
        repository=context.run_repository,
        providers=context.providers,
        event_leases=context.event_leases,
    )


def _active_leased_run(
    db: Session, run_id: uuid.UUID, owner_token: str
) -> SyncRun:
    """Lock and fence a run before a worker-owned persistence mutation."""

    run = db.scalar(
        select(SyncRun)
        .where(
            SyncRun.id == run_id,
            SyncRun.status == SyncRunStatus.RUNNING,
            SyncRun.lease_owner == owner_token[:200],
            SyncRun.lease_expires_at.is_not(None),
            SyncRun.lease_expires_at > _utcnow(),
        )
        .with_for_update()
    )
    if run is None:
        raise ConcurrentModificationError(
            "Run lease is expired or owned by another execution",
            run_id=str(run_id),
        )
    return run


def _profile_config(profile: SyncProfile, *, revision: int) -> ProfileConfig:
    selectors = tuple(
        EventSelector(
            name_contains=rule.get("name_contains"),
            name_regex=rule.get("name_regex"),
            campus_ids=tuple(str(value) for value in rule.get("campus_ids") or ()),
            calendar_ids=tuple(str(value) for value in rule.get("calendar_ids") or ()),
        )
        for rule in canonicalize_persisted_event_rules(profile.event_rules)
    )
    placements = tuple(
        PlacementRule(
            id=str(rule["id"]),
            anchor=AgendaAnchor(
                item_id=(str(rule["anchor"]["item_id"]) if rule["anchor"].get("item_id") else None),
                item_type=rule["anchor"].get("item_type"),
                title=rule["anchor"].get("title"),
            ),
            relation=AnchorRelation(str(rule.get("relation") or "after")),
            song_start=int(rule.get("song_start", 0)),
            song_end=(int(rule["song_end"]) if rule.get("song_end") is not None else None),
        )
        for rule in profile.placements or ()
    )
    return ProfileConfig(
        id=str(profile.id),
        revision=revision,
        source_timezone=profile.source_timezone,
        target_timezone=profile.target_timezone,
        match_mode=MatchMode(profile.match_mode),
        selectors=selectors,
        placements=placements,
        auto_create_songs=profile.create_missing_songs,
        song_category_id=profile.song_category_id,
        arrangement_name=profile.arrangement_name,
        agenda_item_defaults=dict(profile.agenda_item_defaults or {}),
        lookahead_days=profile.lookahead_days,
    )


def _client_from_connection(
    connection: ProviderConnection, credentials: Mapping[str, Any]
) -> ChurchToolsClient | WorshipToolsClient:
    if connection.provider == ProviderType.CHURCHTOOLS:
        if not connection.base_url:
            raise AuthenticationError("ChurchTools base URL is not configured")
        return ChurchToolsClient(connection.base_url, _churchtools_token(credentials))
    if connection.provider == ProviderType.WORSHIPTOOLS:
        return WorshipToolsClient(
            _required_secret(credentials, "email", "WorshipTools email"),
            _required_secret(
                credentials, "password", "WorshipTools password", strip=False
            ),
            _account_id(connection, credentials),
            str(connection.settings_json.get("timezone") or "UTC"),
        )
    raise ValueError("unsupported provider")


def _account_id(
    connection: ProviderConnection, credentials: Mapping[str, Any]
) -> str:
    value = str(credentials.get("account_id") or "").strip()
    if not value:
        raise AuthenticationError("WorshipTools account_id is not configured")
    return value


def _connection_configuration_error(
    connection: ProviderConnection, credentials: Mapping[str, Any]
) -> str | None:
    if connection.provider == ProviderType.WORSHIPTOOLS:
        labels = {
            "email": "E-Mail-Adresse",
            "password": "Passwort",
            "account_id": "Account-ID",
        }
        missing = [
            label
            for key, label in labels.items()
            if not str(credentials.get(key) or "").strip()
        ]
        if missing:
            return "WorshipTools-Zugangsdaten unvollständig: " + ", ".join(missing) + "."
        return None
    if connection.provider == ProviderType.CHURCHTOOLS:
        if not connection.base_url:
            return "ChurchTools-Basis-URL fehlt."
        token = str(
            credentials.get("token") or credentials.get("login_token") or ""
        ).strip()
        if not token or token.casefold() == "login":
            return "ChurchTools-Login-Token fehlt."
        return None
    return "Der Provider wird nicht unterstützt."


def _churchtools_token(credentials: Mapping[str, Any]) -> str:
    token = str(credentials.get("token") or credentials.get("login_token") or "").strip()
    if token.casefold().startswith("login "):
        token = token[6:].strip()
    if not token:
        raise AuthenticationError("ChurchTools login token is not configured")
    return token


def _required_secret(
    credentials: Mapping[str, Any], key: str, label: str, *, strip: bool = True
) -> str:
    value = str(credentials.get(key) or "")
    if not value.strip():
        raise AuthenticationError(f"{label} is not configured")
    return value.strip() if strip else value


def _planned_target_id(action: PlannedAction) -> str | None:
    value = (
        action.payload.get("agenda_item_id")
        or action.payload.get("target_song_id")
        or action.payload.get("target_event_id")
    )
    return str(value)[:200] if value is not None else None


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
