"""In-memory ports for domain tests and local provider-fixture replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .errors import ConcurrentModificationError, ConflictError, NotFoundError
from .matching import match_song, normalize_text
from .models import (
    ActionExecution,
    ActionStatus,
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
from .ports import SourceProvider, TargetProvider


class FakeSourceProvider(SourceProvider):
    def __init__(self, events: Sequence[SourceEvent], songs: Sequence[SourceSong]) -> None:
        self.events = tuple(events)
        self.songs = tuple(songs)

    async def list_events(self, start: datetime, end: datetime) -> Sequence[SourceEvent]:
        return self.events

    async def list_songs(self) -> Sequence[SourceSong]:
        return self.songs


class FakeTargetProvider(TargetProvider):
    def __init__(
        self,
        events: Sequence[TargetEvent],
        songs: Sequence[TargetSong],
        agendas: Mapping[str, Agenda],
        *,
        before_write: Callable[[str, str], None] | None = None,
        fail_event_ids: set[str] | None = None,
    ) -> None:
        self.events = tuple(events)
        self.songs = {song.id: song for song in songs}
        self.agendas = dict(agendas)
        self.before_write = before_write
        self.fail_event_ids = fail_event_ids or set()
        self.writes: list[tuple[str, str]] = []
        self._counter = 1000

    async def list_events(self, start: datetime, end: datetime) -> Sequence[TargetEvent]:
        return self.events

    async def list_songs(self) -> Sequence[TargetSong]:
        return tuple(self.songs.values())

    async def get_song(self, song_id: str) -> TargetSong:
        try:
            return self.songs[song_id]
        except KeyError as exc:
            raise NotFoundError("Fake target song not found", song_id=song_id) from exc

    async def get_agenda(self, event_id: str) -> Agenda:
        try:
            return self.agendas[event_id]
        except KeyError as exc:
            raise NotFoundError("Fake target agenda not found", event_id=event_id) from exc

    async def create_song(self, payload: Mapping[str, Any], action_id: str) -> TargetSong:
        self._write("create_song", "", action_id)
        probe = SourceSong("probe", str(payload["name"]), str(payload.get("author") or ""), payload.get("ccli"))
        matched = match_song(probe, tuple(self.songs.values()))
        if matched.target:
            return matched.target
        song_id = self._next_id("song")
        arrangement = Arrangement(self._next_id("arrangement"), str(payload["arrangement_name"]), True)
        song = TargetSong(song_id, probe.name, probe.artist, probe.ccli, (arrangement,))
        self.songs[song.id] = song
        return song

    async def create_arrangement(self, song_id: str, name: str, action_id: str) -> Arrangement:
        self._write("create_arrangement", "", action_id)
        song = await self.get_song(song_id)
        existing = [item for item in song.arrangements if normalize_text(item.name) == normalize_text(name)]
        if len(existing) == 1:
            return existing[0]
        if len(existing) > 1:
            raise ConflictError("Fake arrangement is ambiguous")
        arrangement = Arrangement(self._next_id("arrangement"), name, True)
        self.songs[song_id] = replace(song, arrangements=song.arrangements + (arrangement,))
        return arrangement

    async def insert_agenda_song(
        self,
        event_id: str,
        arrangement_id: str,
        defaults: Mapping[str, Any],
        action_id: str,
        *,
        before_item_id: str | None = None,
        after_item_id: str | None = None,
    ) -> AgendaItem:
        self._write("insert_item", event_id, action_id)
        agenda = await self.get_agenda(event_id)
        items = list(agenda.items)
        if before_item_id:
            position = next(index for index, item in enumerate(items) if item.id == before_item_id)
        elif after_item_id:
            position = next(index for index, item in enumerate(items) if item.id == after_item_id) + 1
        else:
            position = len(items)
        item = AgendaItem(
            self._next_id("item"),
            position,
            "song",
            str(defaults.get("title") or ""),
            self._song_for_arrangement(arrangement_id),
            arrangement_id,
        )
        items.insert(position, item)
        self._store_agenda(event_id, items)
        return next(value for value in self.agendas[event_id].items if value.id == item.id)

    async def replace_agenda_song(
        self,
        event_id: str,
        item_id: str,
        arrangement_id: str,
        defaults: Mapping[str, Any],
        action_id: str,
    ) -> AgendaItem:
        self._write("replace_item", event_id, action_id)
        agenda = await self.get_agenda(event_id)
        items = list(agenda.items)
        index = next(index for index, item in enumerate(items) if item.id == item_id)
        items[index] = AgendaItem(
            item_id,
            index,
            "song",
            str(defaults.get("title") or ""),
            self._song_for_arrangement(arrangement_id),
            arrangement_id,
        )
        self._store_agenda(event_id, items)
        return items[index]

    async def delete_agenda_item(self, event_id: str, item_id: str, action_id: str) -> None:
        self._write("delete_item", event_id, action_id)
        agenda = await self.get_agenda(event_id)
        self._store_agenda(event_id, [item for item in agenda.items if item.id != item_id])

    def _write(self, kind: str, event_id: str, action_id: str) -> None:
        if event_id in self.fail_event_ids:
            raise ConflictError("Injected fake provider failure", event_id=event_id)
        if self.before_write:
            self.before_write(kind, action_id)
        self.writes.append((kind, action_id))

    def _song_for_arrangement(self, arrangement_id: str) -> str:
        for song in self.songs.values():
            if any(item.id == arrangement_id for item in song.arrangements):
                return song.id
        raise NotFoundError("Fake arrangement not found", arrangement_id=arrangement_id)

    def _store_agenda(self, event_id: str, items: Sequence[AgendaItem]) -> None:
        self.agendas[event_id] = Agenda(
            event_id, tuple(replace(item, position=index) for index, item in enumerate(items))
        )

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"


class StaticProviderRegistry:
    def __init__(self, source: SourceProvider, target: TargetProvider) -> None:
        self._source = source
        self._target = target

    def source(self, workspace_id: str, connection_id: str, source_timezone: str) -> SourceProvider:
        return self._source

    def target(
        self, workspace_id: str, connection_id: str, target_timezone: str
    ) -> TargetProvider:
        return self._target


class MemoryRunRepository:
    def __init__(
        self,
        specification: RunSpecification,
        ownerships: Sequence[Ownership] = (),
        event_sync_states: Sequence[EventSyncCheckpoint] = (),
    ) -> None:
        self.specification = specification
        self._claimed = False
        self.owner_token: str | None = None
        self.plan: SyncPlan | None = None
        self.executions: dict[str, ActionExecution] = {}
        self.ownership_rows = list(ownerships)
        self.event_sync_rows = {
            (row.source_event_id, row.target_event_id): row
            for row in event_sync_states
        }
        self.status: RunStatus | None = None
        self.error: Mapping[str, Any] | None = None
        self.cancel = False
        self.sequence: list[str] = []
        self.renewals = 0

    async def claim(self, run_id: str, worker_id: str, lease_seconds: int) -> RunSpecification | None:
        if self._claimed or run_id != self.specification.run_id:
            return None
        self._claimed = True
        self.owner_token = worker_id
        self.sequence.append("claim")
        return self.specification

    async def renew_lease(self, run_id: str, worker_id: str, lease_seconds: int) -> bool:
        owned = worker_id == self.owner_token
        if owned:
            self.renewals += 1
        return owned

    async def ownerships(self, profile_id: str, target_event_id: str) -> Sequence[Ownership]:
        return tuple(
            row for row in self.ownership_rows if row.target_event_id == target_event_id
        )

    async def event_sync_states(
        self, profile_id: str, event_keys: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], EventSyncCheckpoint]:
        return {
            key: self.event_sync_rows[key]
            for key in event_keys
            if key in self.event_sync_rows
        }

    async def record_event_synced(
        self, run_id: str, event: EventPlan, owner_token: str
    ) -> None:
        self._assert_owner(owner_token)
        if (
            event.target_event_id is None
            or event.source_fingerprint is None
            or event.config_fingerprint is None
        ):
            return
        key = (event.source_event_id, event.target_event_id)
        self.event_sync_rows[key] = EventSyncCheckpoint(
            source_event_id=event.source_event_id,
            target_event_id=event.target_event_id,
            source_fingerprint=event.source_fingerprint,
            config_fingerprint=event.config_fingerprint,
        )
        self.sequence.append(f"record_event_synced:{event.source_event_id}")

    async def persist_plan(
        self, run_id: str, plan: SyncPlan, owner_token: str
    ) -> None:
        self._assert_owner(owner_token)
        self.plan = plan
        self.sequence.append("persist_plan")

    async def load_plan(self, run_id: str) -> SyncPlan | None:
        return self.plan

    async def prior_executions(self, run_id: str) -> Mapping[str, ActionExecution]:
        return dict(self.executions)

    async def action_started(
        self, run_id: str, action: PlannedAction, owner_token: str
    ) -> None:
        self._assert_owner(owner_token)
        if self.plan is None:
            raise AssertionError("action started before plan persistence")
        self.sequence.append(f"start:{action.id}")

    async def action_finished(
        self, run_id: str, execution: ActionExecution, owner_token: str
    ) -> None:
        self._assert_owner(owner_token)
        self.executions[execution.action_id] = execution
        self.sequence.append(f"{execution.status.value}:{execution.action_id}")

    async def bind_ownership(
        self, run_id: str, ownership: Ownership, owner_token: str
    ) -> None:
        self._assert_owner(owner_token)
        self.ownership_rows.append(ownership)

    async def unbind_ownership(
        self,
        run_id: str,
        profile_id: str,
        target_event_id: str,
        agenda_item_id: str,
        owner_token: str,
    ) -> None:
        self._assert_owner(owner_token)
        self.ownership_rows = [
            row
            for row in self.ownership_rows
            if not (
                row.profile_id == profile_id
                and row.target_event_id == target_event_id
                and row.agenda_item_id == agenda_item_id
            )
        ]

    async def cancel_requested(self, run_id: str) -> bool:
        return self.cancel

    async def finish(
        self,
        run_id: str,
        status: RunStatus,
        error: Mapping[str, Any] | None = None,
        *,
        worker_id: str | None = None,
    ) -> bool:
        if worker_id != self.owner_token:
            return False
        self.status = status
        self.error = error
        self.sequence.append(f"finish:{status.value}")
        return True

    def _assert_owner(self, owner_token: str) -> None:
        if owner_token != self.owner_token:
            raise ConcurrentModificationError("Run lease was taken over")


class MemoryEventLeaseManager:
    def __init__(self) -> None:
        self.held: dict[tuple[str, str], str] = {}

    async def acquire(self, connection_id: str, event_id: str, owner_token: str, ttl_seconds: int) -> bool:
        key = (connection_id, event_id)
        if key in self.held:
            return False
        self.held[key] = owner_token
        return True

    async def release(self, connection_id: str, event_id: str, owner_token: str) -> None:
        key = (connection_id, event_id)
        if self.held.get(key) == owner_token:
            self.held.pop(key, None)

    async def renew(self, connection_id: str, event_id: str, owner_token: str, ttl_seconds: int) -> bool:
        return self.held.get((connection_id, event_id)) == owner_token
