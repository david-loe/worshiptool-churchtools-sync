"""Ports implemented by HTTP adapters and the PostgreSQL job repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .models import (
    ActionExecution,
    Agenda,
    AgendaItem,
    Arrangement,
    EventPlan,
    EventSyncCheckpoint,
    Ownership,
    PlannedAction,
    RunSpecification,
    RunStatus,
    SourceEvent,
    SourceSong,
    SyncPlan,
    TargetEvent,
    TargetSong,
)


@runtime_checkable
class SourceProvider(Protocol):
    async def list_events(self, start: datetime, end: datetime) -> Sequence[SourceEvent]: ...

    async def list_songs(self) -> Sequence[SourceSong]: ...


@runtime_checkable
class TargetProvider(Protocol):
    async def list_events(self, start: datetime, end: datetime) -> Sequence[TargetEvent]: ...

    async def list_songs(self) -> Sequence[TargetSong]: ...

    async def get_song(self, song_id: str) -> TargetSong: ...

    async def get_agenda(self, event_id: str) -> Agenda: ...

    async def create_song(self, payload: Mapping[str, Any], action_id: str) -> TargetSong: ...

    async def create_arrangement(self, song_id: str, name: str, action_id: str) -> Arrangement: ...

    async def insert_agenda_song(
        self,
        event_id: str,
        arrangement_id: str,
        defaults: Mapping[str, Any],
        action_id: str,
        *,
        before_item_id: str | None = None,
        after_item_id: str | None = None,
    ) -> AgendaItem: ...

    async def replace_agenda_song(
        self,
        event_id: str,
        item_id: str,
        arrangement_id: str,
        defaults: Mapping[str, Any],
        action_id: str,
    ) -> AgendaItem: ...

    async def delete_agenda_item(self, event_id: str, item_id: str, action_id: str) -> None: ...


class ProviderRegistry(Protocol):
    def source(self, workspace_id: str, connection_id: str, source_timezone: str) -> SourceProvider: ...

    def target(
        self, workspace_id: str, connection_id: str, target_timezone: str
    ) -> TargetProvider: ...


class RunRepository(Protocol):
    """Transactional integration boundary for SQLAlchemy/PostgreSQL.

    Implementations must scope every operation by the run's workspace, use a
    compare-and-set lease in ``claim``, and commit ``persist_plan`` before any
    provider mutation is allowed.
    """

    async def claim(self, run_id: str, worker_id: str, lease_seconds: int) -> RunSpecification | None: ...

    async def renew_lease(self, run_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    async def ownerships(self, profile_id: str, target_event_id: str) -> Sequence[Ownership]: ...

    async def event_sync_states(
        self, profile_id: str, event_keys: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], EventSyncCheckpoint]: ...

    async def record_event_synced(
        self, run_id: str, event: EventPlan, owner_token: str
    ) -> None: ...

    async def load_plan(self, run_id: str) -> SyncPlan | None: ...

    async def persist_plan(
        self, run_id: str, plan: SyncPlan, owner_token: str
    ) -> None: ...

    async def prior_executions(self, run_id: str) -> Mapping[str, ActionExecution]: ...

    async def action_started(
        self, run_id: str, action: PlannedAction, owner_token: str
    ) -> None: ...

    async def action_finished(
        self, run_id: str, execution: ActionExecution, owner_token: str
    ) -> None: ...

    async def bind_ownership(
        self, run_id: str, ownership: Ownership, owner_token: str
    ) -> None: ...

    async def unbind_ownership(
        self,
        run_id: str,
        profile_id: str,
        target_event_id: str,
        agenda_item_id: str,
        owner_token: str,
    ) -> None: ...

    async def cancel_requested(self, run_id: str) -> bool: ...

    async def finish(
        self,
        run_id: str,
        status: RunStatus,
        error: Mapping[str, Any] | None = None,
        *,
        worker_id: str | None = None,
    ) -> bool: ...


class RunDispatcher(Protocol):
    async def enqueue(self, run_id: str) -> None: ...


class DueRunRepository(Protocol):
    async def create_due_runs(self, now: datetime, limit: int) -> Sequence[str]: ...

    async def mark_dispatched(self, run_id: str, dispatched_at: datetime) -> bool: ...

    async def notification_work_due(self, now: datetime) -> bool: ...


class EventLeaseManager(Protocol):
    """Distributed per-target-event lease (normally implemented with Redis)."""

    async def acquire(
        self, target_connection_id: str, target_event_id: str, owner_token: str, ttl_seconds: int
    ) -> bool: ...

    async def renew(
        self, target_connection_id: str, target_event_id: str, owner_token: str, ttl_seconds: int
    ) -> bool: ...

    async def release(self, target_connection_id: str, target_event_id: str, owner_token: str) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
