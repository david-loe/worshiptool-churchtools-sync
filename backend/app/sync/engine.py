"""Plan/apply/verify orchestration for an at-least-once worker queue."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .errors import ConcurrentModificationError, NotFoundError, SyncError
from .matching import normalize_ccli, normalize_text
from .models import (
    ActionExecution,
    ActionKind,
    ActionStatus,
    Agenda,
    EventPlan,
    EventPlanStatus,
    Ownership,
    PlannedAction,
    RunStatus,
    SyncPlan,
)
from .planner import SyncPlanner
from .ports import Clock, EventLeaseManager, ProviderRegistry, RunRepository, TargetProvider

logger = logging.getLogger(__name__)


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class NoopEventLeaseManager:
    """Development fallback; production wiring should inject a Redis lease."""

    async def acquire(
        self, target_connection_id: str, target_event_id: str, run_id: str, ttl_seconds: int
    ) -> bool:
        return True

    async def release(self, target_connection_id: str, target_event_id: str, run_id: str) -> None:
        return None

    async def renew(
        self, target_connection_id: str, target_event_id: str, run_id: str, ttl_seconds: int
    ) -> bool:
        return True


@dataclass(slots=True)
class _RunProgress:
    resources: dict[str, str]
    verified_actions: int = 0
    event_successes: int = 0
    event_failures: int = 0


class SyncOrchestrator:
    def __init__(
        self,
        repository: RunRepository,
        providers: ProviderRegistry,
        *,
        planner: SyncPlanner | None = None,
        clock: Clock | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        event_leases: EventLeaseManager | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.planner = planner or SyncPlanner()
        self.clock = clock or UtcClock()
        self.worker_id = worker_id or socket.gethostname()
        self.lease_seconds = lease_seconds
        self.event_leases = event_leases or NoopEventLeaseManager()

    async def execute(self, run_id: str) -> RunStatus | None:
        owner_token = (
            f"{uuid.uuid4().hex}:{self.worker_id[:80]}:"
            f"{os.getpid()}:{threading.get_ident()}"
        )
        specification = await self.repository.claim(
            run_id, owner_token, self.lease_seconds
        )
        if specification is None:
            return None
        source = None
        target = None
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_run_lease(run_id, owner_token, heartbeat_stop)
        )
        try:
            target = self.providers.target(
                specification.workspace_id,
                specification.target_connection_id,
                specification.profile.target_timezone,
            )
            plan = await self.repository.load_plan(run_id)
            if plan is None:
                source = self.providers.source(
                    specification.workspace_id,
                    specification.source_connection_id,
                    specification.profile.source_timezone,
                )
                now = self.clock.now()
                end = now + timedelta(days=specification.profile.lookahead_days)
                source_events = await source.list_events(now, end)
                target_events = await target.list_events(now, end)
                source_songs = await source.list_songs()
                target_songs = await target.list_songs()

                # Fetch only agendas that can possibly be selected.  The planner
                # still repeats all matching and ambiguity checks itself.
                from .matching import match_events

                preliminary = match_events(specification.profile, tuple(source_events), tuple(target_events))
                target_ids = sorted({match.target.id for match in preliminary if match.target is not None})
                agendas: dict[str, Agenda] = {}
                ownerships: dict[str, Sequence[Ownership]] = {}
                for target_event_id in target_ids:
                    try:
                        agendas[target_event_id] = await target.get_agenda(
                            target_event_id
                        )
                    except NotFoundError:
                        # ChurchTools returns 404 for otherwise valid events
                        # that do not have an agenda.  Leaving the agenda out
                        # lets the planner record an explicit event-level skip.
                        continue
                    ownerships[target_event_id] = await self.repository.ownerships(
                        specification.profile.id, target_event_id
                    )

                plan = self.planner.plan(
                    run_id=run_id,
                    profile=specification.profile,
                    created_at=now,
                    source_events=source_events,
                    target_events=target_events,
                    source_songs=source_songs,
                    target_songs=target_songs,
                    agendas=agendas,
                    ownerships=ownerships,
                )
                # This commit is the hard boundary: no provider write happens
                # before the complete plan is durable.
                await self.repository.persist_plan(run_id, plan, owner_token)
            elif (
                plan.run_id != run_id
                or plan.profile_id != specification.profile.id
                or plan.profile_revision != specification.profile.revision
            ):
                raise ConcurrentModificationError(
                    "Persisted plan does not match the claimed run specification"
                )
            if specification.dry_run:
                status = _dry_run_status(plan)
                finished = await self.repository.finish(
                    run_id, status, worker_id=owner_token
                )
                return status if finished else None
            status = await self._apply_plan(
                plan, target, specification.target_connection_id, owner_token
            )
            finished = await self.repository.finish(
                run_id, status, worker_id=owner_token
            )
            return status if finished else None
        except SyncError as exc:
            logger.warning("sync run %s failed: %s", run_id, exc.kind.value)
            finished = await self.repository.finish(
                run_id, RunStatus.FAILED, exc.as_dict(), worker_id=owner_token
            )
            return RunStatus.FAILED if finished else None
        except Exception as exc:  # do not turn unexpected defects into success
            # Provider and database exceptions can embed response bodies,
            # request parameters, or credentials. Keep logs diagnosable without
            # serializing the exception or traceback.
            logger.error(
                "unexpected sync run failure",
                extra={"run_id": run_id, "error_type": type(exc).__name__},
            )
            finished = await self.repository.finish(
                run_id,
                RunStatus.FAILED,
                {"kind": "internal", "message": "Unbehandelter interner Sync-Fehler", "retryable": False},
                worker_id=owner_token,
            )
            return RunStatus.FAILED if finished else None
        finally:
            heartbeat_stop.set()
            await heartbeat
            await _maybe_close(source)
            await _maybe_close(target)

    async def _apply_plan(
        self,
        plan: SyncPlan,
        target: TargetProvider,
        target_connection_id: str,
        owner_token: str,
    ) -> RunStatus:
        prior = dict(await self.repository.prior_executions(plan.run_id))
        progress = _RunProgress(resources={})

        catalog_lease = "__song_catalog__"
        catalog_locked = not plan.preparation_actions or await self.event_leases.acquire(
            target_connection_id, catalog_lease, owner_token, self.lease_seconds
        )
        if not catalog_locked:
            await self._skip_remaining(
                plan, plan.preparation_actions, "catalog_locked", owner_token
            )
        else:
            catalog_heartbeat_stop = asyncio.Event()
            catalog_heartbeat_lost = asyncio.Event()
            catalog_heartbeat = asyncio.create_task(
                self._heartbeat_remote_lease(
                    plan.run_id,
                    target_connection_id,
                    catalog_lease,
                    owner_token,
                    catalog_heartbeat_stop,
                    catalog_heartbeat_lost,
                )
            )
            try:
                for preparation_index, action in enumerate(plan.preparation_actions):
                    if await self.repository.cancel_requested(plan.run_id):
                        return RunStatus.CANCELED
                    if catalog_heartbeat_lost.is_set():
                        await self._skip_remaining(
                            plan,
                            plan.preparation_actions[preparation_index:],
                            "lease_lost",
                            owner_token,
                        )
                        break
                    if not await self._renew_leases(
                        plan.run_id,
                        target_connection_id,
                        catalog_lease,
                        owner_token,
                    ):
                        await self._skip_remaining(
                            plan,
                            plan.preparation_actions[preparation_index:],
                            "lease_lost",
                            owner_token,
                        )
                        break
                    missing = [dependency for dependency in action.dependencies if dependency not in progress.resources]
                    if missing:
                        execution = ActionExecution(
                            action.id,
                            ActionStatus.SKIPPED,
                            error={"kind": "dependency_failed", "dependencies": missing},
                        )
                        await self.repository.action_finished(
                            plan.run_id, execution, owner_token
                        )
                        continue
                    execution = await self._execute_or_resume(
                        plan,
                        action,
                        target,
                        progress.resources,
                        prior.get(action.id),
                        owner_token,
                        catalog_heartbeat_lost,
                    )
                    if execution.status is ActionStatus.VERIFIED:
                        progress.verified_actions += 1
            finally:
                catalog_heartbeat_stop.set()
                await catalog_heartbeat
                if plan.preparation_actions:
                    await self.event_leases.release(
                        target_connection_id, catalog_lease, owner_token
                    )

        for event in plan.events:
            if event.status is EventPlanStatus.SKIPPED:
                continue
            if event.status is not EventPlanStatus.READY:
                progress.event_failures += 1
                continue
            if await self.repository.cancel_requested(plan.run_id):
                return RunStatus.CANCELED
            assert event.target_event_id is not None
            locked = await self.event_leases.acquire(
                # An event ID is only unique within a target connection.
                # Connection scope prevents two profiles racing the same CT event.
                target_connection_id,
                event.target_event_id,
                owner_token,
                self.lease_seconds,
            )
            if not locked:
                progress.event_failures += 1
                await self._skip_remaining(
                    plan, event.actions, "event_locked", owner_token
                )
                continue
            try:
                success = await self._apply_event(
                    plan,
                    event,
                    target,
                    progress.resources,
                    prior,
                    target_connection_id,
                    owner_token,
                )
            finally:
                await self.event_leases.release(
                    target_connection_id, event.target_event_id, owner_token
                )
            if await self.repository.cancel_requested(plan.run_id):
                return RunStatus.CANCELED
            if success:
                progress.event_successes += 1
            else:
                progress.event_failures += 1

        ready_count = sum(event.status is EventPlanStatus.READY for event in plan.events)
        if ready_count == 0:
            return RunStatus.FAILED if progress.event_failures else RunStatus.SKIPPED
        if progress.event_failures == 0 and progress.event_successes == ready_count:
            return RunStatus.SUCCEEDED
        if progress.event_successes or progress.verified_actions:
            return RunStatus.PARTIAL
        return RunStatus.FAILED

    async def _apply_event(
        self,
        plan: SyncPlan,
        event: EventPlan,
        target: TargetProvider,
        resources: dict[str, str],
        prior: Mapping[str, ActionExecution],
        target_connection_id: str,
        owner_token: str,
    ) -> bool:
        assert event.target_event_id is not None
        expected_fingerprint = event.initial_agenda_fingerprint
        heartbeat_stop = asyncio.Event()
        heartbeat_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_remote_lease(
                plan.run_id,
                target_connection_id,
                event.target_event_id,
                owner_token,
                heartbeat_stop,
                heartbeat_lost,
            )
        )
        try:
            for index, action in enumerate(event.actions):
                if await self.repository.cancel_requested(plan.run_id):
                    return False
                if heartbeat_lost.is_set() or not await self._renew_leases(
                    plan.run_id,
                    target_connection_id,
                    event.target_event_id,
                    owner_token,
                ):
                    await self.repository.action_finished(
                        plan.run_id,
                        ActionExecution(
                            action.id,
                            ActionStatus.FAILED,
                            error={"kind": "lease_lost", "message": "Sync-Lease ging vor dem Apply verloren"},
                        ),
                        owner_token,
                    )
                    await self._skip_remaining(
                        plan,
                        event.actions[index + 1 :],
                        "lease_lost",
                        owner_token,
                    )
                    return False
                missing = [dependency for dependency in action.dependencies if dependency not in resources]
                if missing:
                    execution = ActionExecution(
                        action.id,
                        ActionStatus.SKIPPED,
                        error={
                            "kind": "dependency_failed",
                            "message": "Abhängige Provider-Ressource wurde nicht verifiziert",
                            "dependencies": missing,
                        },
                    )
                    await self.repository.action_finished(
                        plan.run_id, execution, owner_token
                    )
                    await self._skip_remaining(
                        plan,
                        event.actions[index + 1 :],
                        "event_dependency_failed",
                        owner_token,
                    )
                    return False

                previous = prior.get(action.id)
                if previous and previous.status is ActionStatus.VERIFIED:
                    execution = await self._execute_or_resume(
                        plan,
                        action,
                        target,
                        resources,
                        previous,
                        owner_token,
                        heartbeat_lost,
                    )
                    if execution.status is not ActionStatus.VERIFIED:
                        await self._skip_remaining(
                            plan,
                            event.actions[index + 1 :],
                            "event_reverification_failed",
                            owner_token,
                        )
                        return False
                    expected_fingerprint = str(
                        execution.result.get("agenda_fingerprint")
                        or expected_fingerprint
                    )
                    continue

                if action.mutates_agenda and not (previous and previous.status is ActionStatus.APPLIED):
                    actual = await target.get_agenda(event.target_event_id)
                    if actual.fingerprint != expected_fingerprint:
                        reconciled = _reconcile_agenda_effect(action, actual, resources)
                        if reconciled is not None:
                            applied = ActionExecution(
                                action.id, ActionStatus.APPLIED, result=reconciled
                            )
                            await self.repository.action_finished(
                                plan.run_id, applied, owner_token
                            )
                            execution = await self._execute_or_resume(
                                plan,
                                action,
                                target,
                                resources,
                                applied,
                                owner_token,
                                heartbeat_lost,
                            )
                            if execution.status is not ActionStatus.VERIFIED:
                                await self._skip_remaining(
                                    plan,
                                    event.actions[index + 1 :],
                                    "event_reconciliation_failed",
                                    owner_token,
                                )
                                return False
                            expected_fingerprint = str(
                                execution.result.get("agenda_fingerprint") or actual.fingerprint
                            )
                            continue
                        error = ConcurrentModificationError(
                            event_id=event.target_event_id,
                            expected_fingerprint=expected_fingerprint,
                            actual_fingerprint=actual.fingerprint,
                        )
                        await self.repository.action_finished(
                            plan.run_id,
                            ActionExecution(
                                action.id, ActionStatus.FAILED, error=error.as_dict()
                            ),
                            owner_token,
                        )
                        await self._skip_remaining(
                            plan,
                            event.actions[index + 1 :],
                            "agenda_changed",
                            owner_token,
                        )
                        return False

                execution = await self._execute_or_resume(
                    plan,
                    action,
                    target,
                    resources,
                    previous,
                    owner_token,
                    heartbeat_lost,
                )
                if execution.status is not ActionStatus.VERIFIED:
                    await self._skip_remaining(
                        plan,
                        event.actions[index + 1 :],
                        "event_action_failed",
                        owner_token,
                    )
                    return False
                expected_fingerprint = str(execution.result.get("agenda_fingerprint") or expected_fingerprint)
            return True
        finally:
            heartbeat_stop.set()
            await heartbeat

    async def _execute_or_resume(
        self,
        plan: SyncPlan,
        action: PlannedAction,
        target: TargetProvider,
        resources: dict[str, str],
        previous: ActionExecution | None,
        owner_token: str,
        remote_lease_lost: asyncio.Event | None = None,
    ) -> ActionExecution:
        try:
            if previous and previous.status is ActionStatus.VERIFIED:
                verified_result = await self._verify_action(
                    action, target, resources, previous.result
                )
                _ensure_remote_lease(remote_lease_lost)
                execution = ActionExecution(
                    action.id, ActionStatus.VERIFIED, result=verified_result
                )
                self._restore_resources(action, verified_result, resources)
                _ensure_remote_lease(remote_lease_lost)
                await self._update_ownership(
                    plan.run_id, action, verified_result, owner_token
                )
                return execution
            if previous and previous.status in {ActionStatus.FAILED, ActionStatus.SKIPPED}:
                return previous
            # An APPLIED row means a previous worker reached the provider but
            # crashed before verification.  Verify it; never write it again.
            if previous and previous.status is ActionStatus.APPLIED:
                result = dict(previous.result)
            else:
                await self.repository.action_started(
                    plan.run_id, action, owner_token
                )
                result = await self._apply_action(action, target, resources)
                _ensure_remote_lease(remote_lease_lost)
                applied = ActionExecution(action.id, ActionStatus.APPLIED, result=result)
                await self.repository.action_finished(
                    plan.run_id, applied, owner_token
                )

            verified_result = await self._verify_action(action, target, resources, result)
            _ensure_remote_lease(remote_lease_lost)
            execution = ActionExecution(action.id, ActionStatus.VERIFIED, result=verified_result)
            await self.repository.action_finished(
                plan.run_id, execution, owner_token
            )
            self._restore_resources(action, verified_result, resources)
            _ensure_remote_lease(remote_lease_lost)
            await self._update_ownership(
                plan.run_id, action, verified_result, owner_token
            )
            return execution
        except SyncError as exc:
            execution = ActionExecution(action.id, ActionStatus.FAILED, error=exc.as_dict())
            await self.repository.action_finished(
                plan.run_id, execution, owner_token
            )
            return execution
        except Exception as exc:
            logger.error(
                "unexpected sync action failure",
                extra={"action_id": action.id, "error_type": type(exc).__name__},
            )
            execution = ActionExecution(
                action.id,
                ActionStatus.FAILED,
                error={"kind": "internal", "message": "Unbehandelter interner Aktionsfehler", "retryable": False},
            )
            await self.repository.action_finished(
                plan.run_id, execution, owner_token
            )
            return execution

    async def _apply_action(
        self, action: PlannedAction, target: TargetProvider, resources: Mapping[str, str]
    ) -> dict[str, Any]:
        payload = action.payload
        if action.kind is ActionKind.CREATE_SONG:
            song = await target.create_song(payload, action.id)
            arrangement = next((item for item in song.arrangements if item.is_default), None)
            arrangement = arrangement or (song.arrangements[0] if song.arrangements else None)
            if arrangement is None:
                raise ConcurrentModificationError("Atomically created song has no arrangement", song_id=song.id)
            return {
                "song_id": song.id,
                "arrangement_id": arrangement.id,
                "song_resource_key": payload["song_resource_key"],
                "arrangement_resource_key": payload["arrangement_resource_key"],
            }
        if action.kind is ActionKind.CREATE_ARRANGEMENT:
            song_id = _resolve(payload, resources, "target_song_id", "target_song_key")
            arrangement = await target.create_arrangement(song_id, str(payload["name"]), action.id)
            return {
                "song_id": song_id,
                "arrangement_id": arrangement.id,
                "resource_key": payload["resource_key"],
            }
        if action.kind is ActionKind.NOOP:
            return {"agenda_item_id": payload.get("agenda_item_id"), "noop": True}

        event_id = str(payload["target_event_id"])
        if action.kind is ActionKind.INSERT_ITEM:
            arrangement_id = _resolve(payload, resources, "arrangement_id", "arrangement_key")
            before_id = _optional_resolve(payload, resources, "before_item_id", "before_item_key")
            after_id = _optional_resolve(payload, resources, "after_item_id", "after_item_key")
            item = await target.insert_agenda_song(
                event_id,
                arrangement_id,
                dict(payload.get("defaults") or {}),
                action.id,
                before_item_id=before_id,
                after_item_id=after_id,
            )
            return {"agenda_item_id": item.id, "resource_key": payload["resource_key"]}
        if action.kind is ActionKind.REPLACE_ITEM:
            arrangement_id = _resolve(payload, resources, "arrangement_id", "arrangement_key")
            item = await target.replace_agenda_song(
                event_id,
                str(payload["agenda_item_id"]),
                arrangement_id,
                dict(payload.get("defaults") or {}),
                action.id,
            )
            return {"agenda_item_id": item.id}
        if action.kind is ActionKind.DELETE_OWNED_ITEM:
            item_id = str(payload["agenda_item_id"])
            await target.delete_agenda_item(event_id, item_id, action.id)
            return {"agenda_item_id": item_id, "deleted": True}
        raise ValueError(f"unsupported action kind {action.kind}")

    async def _verify_action(
        self,
        action: PlannedAction,
        target: TargetProvider,
        resources: Mapping[str, str],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = action.payload
        verified = dict(result)
        if action.kind is ActionKind.CREATE_SONG:
            song = await target.get_song(str(result["song_id"]))
            if normalize_text(song.name) != normalize_text(str(payload["name"])):
                raise ConcurrentModificationError("Created song verification failed", song_id=song.id)
            expected_ccli = normalize_ccli(payload.get("ccli"))
            if expected_ccli and normalize_ccli(song.ccli) != expected_ccli:
                raise ConcurrentModificationError("Created song CCLI verification failed", song_id=song.id)
            if not any(item.id == str(result["arrangement_id"]) for item in song.arrangements):
                raise ConcurrentModificationError("Created song arrangement verification failed", song_id=song.id)
            return verified
        if action.kind is ActionKind.CREATE_ARRANGEMENT:
            song = await target.get_song(str(result["song_id"]))
            if not any(
                arrangement.id == str(result["arrangement_id"])
                and normalize_text(arrangement.name) == normalize_text(str(payload["name"]))
                for arrangement in song.arrangements
            ):
                raise ConcurrentModificationError("Created arrangement verification failed", song_id=song.id)
            return verified
        if action.kind is ActionKind.NOOP and payload.get("cleanup_only"):
            return verified

        event_id = str(payload["target_event_id"])
        agenda = await target.get_agenda(event_id)
        item_id = str(result.get("agenda_item_id") or payload.get("agenda_item_id"))
        item = next((candidate for candidate in agenda.items if candidate.id == item_id), None)
        if action.kind is ActionKind.DELETE_OWNED_ITEM:
            if item is not None:
                raise ConcurrentModificationError("Deleted agenda item is still present", item_id=item_id)
        else:
            expected_song_id = _resolve(payload, resources, "target_song_id", "target_song_key")
            expected_arrangement_id = _resolve(
                payload, resources, "arrangement_id", "arrangement_key"
            )
            if (
                item is None
                or item.type != "song"
                or item.song_id != expected_song_id
                or item.arrangement_id != expected_arrangement_id
            ):
                raise ConcurrentModificationError("Agenda song verification failed", item_id=item_id)
            verified["target_song_id"] = expected_song_id
            verified["arrangement_id"] = expected_arrangement_id
        verified["agenda_fingerprint"] = agenda.fingerprint
        return verified

    async def _update_ownership(
        self,
        run_id: str,
        action: PlannedAction,
        result: Mapping[str, Any],
        owner_token: str,
    ) -> None:
        payload = action.payload
        if action.kind is ActionKind.DELETE_OWNED_ITEM or (action.kind is ActionKind.NOOP and payload.get("cleanup_only")):
            await self.repository.unbind_ownership(
                run_id,
                str(payload["profile_id"]),
                str(payload["target_event_id"]),
                str(payload["agenda_item_id"]),
                owner_token,
            )
            return
        if action.kind not in {ActionKind.INSERT_ITEM, ActionKind.REPLACE_ITEM, ActionKind.NOOP}:
            return
        if action.kind is ActionKind.NOOP and not payload.get("already_owned"):
            return
        item_id = str(result.get("agenda_item_id") or payload["agenda_item_id"])
        await self.repository.bind_ownership(
            run_id,
            Ownership(
                profile_id=str(payload["profile_id"]),
                target_event_id=str(payload["target_event_id"]),
                agenda_item_id=item_id,
                source_key=str(payload["source_key"]),
                placement_id=str(payload["placement_id"]),
                fingerprint={
                    key: value
                    for key, value in {
                        "agenda_fingerprint": result.get("agenda_fingerprint"),
                        "target_song_id": result.get("target_song_id"),
                        "arrangement_id": result.get("arrangement_id"),
                    }.items()
                    if value is not None
                },
            ),
            owner_token,
        )

    async def _skip_remaining(
        self,
        plan: SyncPlan,
        actions: Sequence[PlannedAction],
        reason: str,
        owner_token: str,
    ) -> None:
        for action in actions:
            await self.repository.action_finished(
                plan.run_id,
                ActionExecution(
                    action.id,
                    ActionStatus.SKIPPED,
                    error={"kind": reason, "message": "Aktion wegen vorherigem Eventfehler übersprungen"},
                ),
                owner_token,
            )

    async def _renew_leases(
        self,
        run_id: str,
        target_connection_id: str,
        remote_resource_id: str,
        owner_token: str,
    ) -> bool:
        run_ok = await self.repository.renew_lease(
            run_id, owner_token, self.lease_seconds
        )
        if not run_ok:
            return False
        return await self.event_leases.renew(
            target_connection_id,
            remote_resource_id,
            owner_token,
            self.lease_seconds,
        )

    async def _heartbeat_run_lease(
        self, run_id: str, owner_token: str, stop: asyncio.Event
    ) -> None:
        interval = max(0.1, min(30.0, self.lease_seconds / 3))
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await self.repository.renew_lease(
                    run_id, owner_token, self.lease_seconds
                )
            except Exception as exc:
                logger.error(
                    "sync run lease heartbeat failed",
                    extra={"run_id": run_id, "error_type": type(exc).__name__},
                )
                return
            if not renewed:
                logger.warning(
                    "sync run lease heartbeat lost ownership",
                    extra={"run_id": run_id},
                )
                return

    async def _heartbeat_remote_lease(
        self,
        run_id: str,
        target_connection_id: str,
        remote_resource_id: str,
        owner_token: str,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        interval = max(0.1, min(30.0, self.lease_seconds / 3))
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await self._renew_leases(
                    run_id,
                    target_connection_id,
                    remote_resource_id,
                    owner_token,
                )
            except Exception as exc:
                lost.set()
                logger.error(
                    "remote sync lease heartbeat failed",
                    extra={
                        "run_id": run_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return
            if not renewed:
                lost.set()
                logger.warning(
                    "remote sync lease heartbeat lost ownership",
                    extra={"run_id": run_id},
                )
                return

    @staticmethod
    def _restore_resources(
        action: PlannedAction, result: Mapping[str, Any], resources: dict[str, str]
    ) -> None:
        if action.kind is ActionKind.CREATE_SONG:
            song_key = result.get("song_resource_key") or action.payload.get("song_resource_key")
            arrangement_key = result.get("arrangement_resource_key") or action.payload.get("arrangement_resource_key")
            if song_key and result.get("song_id") is not None:
                resources[str(song_key)] = str(result["song_id"])
            if arrangement_key and result.get("arrangement_id") is not None:
                resources[str(arrangement_key)] = str(result["arrangement_id"])
            return
        key = result.get("resource_key") or action.payload.get("resource_key")
        if not key:
            return
        if action.kind is ActionKind.CREATE_ARRANGEMENT:
            value = result.get("arrangement_id")
        else:
            value = result.get("agenda_item_id")
        if value is not None:
            resources[str(key)] = str(value)


def _resolve(
    payload: Mapping[str, Any], resources: Mapping[str, str], literal_key: str, resource_key: str
) -> str:
    literal = payload.get(literal_key)
    if literal is not None:
        return str(literal)
    reference = payload.get(resource_key)
    if reference is None or str(reference) not in resources:
        raise ConcurrentModificationError(
            "Planned resource dependency is unavailable", resource_key=reference
        )
    return resources[str(reference)]


def _optional_resolve(
    payload: Mapping[str, Any], resources: Mapping[str, str], literal_key: str, resource_key: str
) -> str | None:
    if payload.get(literal_key) is None and payload.get(resource_key) is None:
        return None
    return _resolve(payload, resources, literal_key, resource_key)


def _ensure_remote_lease(lost: asyncio.Event | None) -> None:
    if lost is not None and lost.is_set():
        raise ConcurrentModificationError(
            "Remote sync lease was lost during provider work"
        )


def _dry_run_status(plan: SyncPlan) -> RunStatus:
    ready = sum(event.status is EventPlanStatus.READY for event in plan.events)
    problems = sum(event.status in {EventPlanStatus.AMBIGUOUS, EventPlanStatus.FAILED} for event in plan.events)
    if ready and problems:
        return RunStatus.PARTIAL
    if ready:
        return RunStatus.SUCCEEDED
    if problems:
        return RunStatus.FAILED
    return RunStatus.SKIPPED


async def _maybe_close(provider: object | None) -> None:
    close = getattr(provider, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception as exc:
            logger.warning(
                "provider cleanup failed",
                extra={"error_type": type(exc).__name__},
            )


def _reconcile_agenda_effect(
    action: PlannedAction, agenda: Agenda, resources: Mapping[str, str]
) -> dict[str, Any] | None:
    """Recognize a mutation committed before a worker crash.

    This is deliberately strict about item identity/position.  A mismatch is
    treated as concurrent user modification and is never overwritten.
    """

    payload = action.payload
    items = tuple(sorted(agenda.items, key=lambda item: (item.position, item.id)))
    if action.kind is ActionKind.DELETE_OWNED_ITEM:
        item_id = str(payload["agenda_item_id"])
        if all(item.id != item_id for item in items):
            return {
                "agenda_item_id": item_id,
                "deleted": True,
                "agenda_fingerprint": agenda.fingerprint,
            }
        return None

    try:
        expected_song_id = _resolve(payload, resources, "target_song_id", "target_song_key")
        expected_arrangement_id = _resolve(
            payload, resources, "arrangement_id", "arrangement_key"
        )
    except SyncError:
        return None

    candidate = None
    if action.kind is ActionKind.REPLACE_ITEM:
        item_id = str(payload["agenda_item_id"])
        candidate = next((item for item in items if item.id == item_id), None)
    elif action.kind is ActionKind.INSERT_ITEM:
        before_id = _optional_resolve(
            payload, resources, "before_item_id", "before_item_key"
        )
        after_id = _optional_resolve(
            payload, resources, "after_item_id", "after_item_key"
        )
        if before_id:
            index = next((index for index, item in enumerate(items) if item.id == before_id), -1)
            candidate = items[index - 1] if index > 0 else None
        elif after_id:
            index = next((index for index, item in enumerate(items) if item.id == after_id), -1)
            candidate = items[index + 1] if 0 <= index < len(items) - 1 else None
        elif len(items) == 1:
            candidate = items[0]
    if (
        candidate is None
        or candidate.id
        in {str(item_id) for item_id in payload.get("initial_agenda_item_ids", ())}
        or candidate.type != "song"
        or candidate.song_id != expected_song_id
        or candidate.arrangement_id != expected_arrangement_id
    ):
        return None
    result: dict[str, Any] = {
        "agenda_item_id": candidate.id,
        "agenda_fingerprint": agenda.fingerprint,
    }
    if action.payload.get("resource_key"):
        result["resource_key"] = action.payload["resource_key"]
    return result
