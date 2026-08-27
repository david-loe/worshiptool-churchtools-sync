"""User-facing aggregation of persisted plans and action executions."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence

from .models import SyncAction, SyncActionStatus, SyncRun, SyncRunStatus
from .schemas import (
    CreatedSongResult,
    RunEventResult,
    RunResultMessage,
    SyncActionStatusCounts,
    SyncRunResult,
)
from .sync.models import ActionKind, EventPlan, EventPlanStatus, PlannedAction, SyncPlan
from .sync.serialization import sync_plan_from_dict


def load_persisted_plan(run: SyncRun) -> SyncPlan | None:
    document = run.plan_json
    if not isinstance(document, Mapping):
        return None
    raw = document.get("plan", document)
    if not isinstance(raw, Mapping):
        return None
    return sync_plan_from_dict(raw)


def build_run_result(run: SyncRun, rows: Sequence[SyncAction]) -> SyncRunResult:
    plan = load_persisted_plan(run)
    row_by_id = {row.id.hex: row for row in rows}
    empty_counts = _action_counts(())
    if plan is None:
        events = _synthetic_results(run)
        totals = Counter(event.status for event in events)
        return SyncRunResult(
            total=len(events),
            planned=totals["planned"],
            verified=totals["verified"],
            skipped=totals["skipped"],
            failed=totals["failed"],
            events=events,
            preparation_action_counts=empty_counts,
            preparation_action_total=0,
        )

    preparation_rows = _rows_for_actions(plan.preparation_actions, row_by_id)
    songs = _verified_created_songs(plan.preparation_actions, row_by_id)
    events = [
        _event_result(run, event, row_by_id, songs)
        for event in plan.events
    ]
    totals = Counter(event.status for event in events)
    return SyncRunResult(
        total=len(events),
        planned=totals["planned"],
        verified=totals["verified"],
        skipped=totals["skipped"],
        failed=totals["failed"],
        events=events,
        preparation_action_counts=_action_counts(preparation_rows),
        preparation_action_total=len(preparation_rows),
    )


def action_ids_for_event(plan: SyncPlan, event_plan_id: str) -> tuple[uuid.UUID, ...] | None:
    event = next((item for item in plan.events if item.id == event_plan_id), None)
    if event is None:
        return None
    return tuple(uuid.UUID(action.id) for action in event.actions)


def preparation_action_ids(plan: SyncPlan) -> tuple[uuid.UUID, ...]:
    return tuple(uuid.UUID(action.id) for action in plan.preparation_actions)


def _event_result(
    run: SyncRun,
    event: EventPlan,
    row_by_id: Mapping[str, SyncAction],
    created_songs: Mapping[str, tuple[CreatedSongResult, frozenset[str]]],
) -> RunEventResult:
    rows = _rows_for_actions(event.actions, row_by_id)
    status = _event_status(run, event, rows)
    messages = [
        RunResultMessage(
            code=issue.code,
            message=issue.message,
            severity=issue.severity.value,
            phase="plan",
            details=dict(issue.details),
        )
        for issue in event.issues
    ]
    for row in rows:
        if row.error_json:
            error = dict(row.error_json)
            messages.append(
                RunResultMessage(
                    code=str(error.get("kind") or error.get("code") or "action_error"),
                    message=str(
                        error.get("message")
                        or error.get("detail")
                        or "Eine Aktion konnte nicht erfolgreich abgeschlossen werden."
                    ),
                    severity=(
                        "error"
                        if row.status
                        in {SyncActionStatus.FAILED, SyncActionStatus.SKIPPED}
                        else "warning"
                    ),
                    phase="execution",
                    details=error,
                )
            )
    dependencies = {
        dependency
        for action in event.actions
        for dependency in action.dependencies
    }
    event_songs = [
        song
        for song, resource_keys in created_songs.values()
        if dependencies.intersection(resource_keys)
    ]
    return RunEventResult(
        id=event.id,
        status=status,
        source_event_id=event.source_event_id,
        target_event_id=event.target_event_id,
        source_event_name=event.source_event_name,
        source_event_starts_at=(
            list(event.source_event_starts_at)
            if event.source_event_starts_at is not None
            else None
        ),
        target_event_name=event.target_event_name,
        target_event_starts_at=event.target_event_starts_at,
        messages=messages,
        action_counts=_action_counts(rows),
        action_total=len(rows),
        new_songs=event_songs,
    )


def _event_status(
    run: SyncRun, event: EventPlan, rows: Sequence[SyncAction]
) -> str:
    if event.status in {EventPlanStatus.FAILED, EventPlanStatus.AMBIGUOUS}:
        return "failed"
    if event.status is EventPlanStatus.SKIPPED:
        return "skipped"
    statuses = [row.status for row in rows]
    if any(
        status in {SyncActionStatus.FAILED, SyncActionStatus.SKIPPED}
        for status in statuses
    ):
        return "failed"
    if run.dry_run:
        return "planned"
    if statuses and all(status is SyncActionStatus.VERIFIED for status in statuses):
        return "verified"
    if run.status is SyncRunStatus.CANCELED:
        return (
            "skipped"
            if not any(
                status in {SyncActionStatus.APPLIED, SyncActionStatus.VERIFIED}
                for status in statuses
            )
            else "failed"
        )
    if run.status is SyncRunStatus.SUCCEEDED and not statuses:
        return "verified"
    if run.status in {SyncRunStatus.FAILED, SyncRunStatus.PARTIAL} and statuses and all(
        status is SyncActionStatus.PLANNED for status in statuses
    ):
        return "failed"
    return "planned"


def _verified_created_songs(
    actions: Sequence[PlannedAction], row_by_id: Mapping[str, SyncAction]
) -> dict[str, tuple[CreatedSongResult, frozenset[str]]]:
    result: dict[str, tuple[CreatedSongResult, frozenset[str]]] = {}
    for action in actions:
        if action.kind is not ActionKind.CREATE_SONG:
            continue
        row = row_by_id.get(action.id)
        if row is None or row.status is not SyncActionStatus.VERIFIED:
            continue
        payload = action.payload
        fingerprint = row.fingerprint_json or {}
        keys = frozenset(
            str(value)
            for value in (
                payload.get("song_resource_key"),
                payload.get("arrangement_resource_key"),
            )
            if value
        )
        result[action.id] = (
            CreatedSongResult(
                action_id=action.id,
                source_song_id=(
                    str(payload["source_song_id"])
                    if payload.get("source_song_id") is not None
                    else None
                ),
                target_song_id=(
                    str(fingerprint["song_id"])
                    if fingerprint.get("song_id") is not None
                    else None
                ),
                name=str(payload.get("name") or "Unbenannter Song"),
                author=str(payload.get("author") or ""),
                ccli=str(payload["ccli"]) if payload.get("ccli") else None,
            ),
            keys,
        )
    return result


def _rows_for_actions(
    actions: Sequence[PlannedAction], row_by_id: Mapping[str, SyncAction]
) -> list[SyncAction]:
    return [row_by_id[action.id] for action in actions if action.id in row_by_id]


def _action_counts(rows: Sequence[SyncAction]) -> SyncActionStatusCounts:
    counts = Counter(row.status.value for row in rows)
    return SyncActionStatusCounts(**counts)


def _synthetic_results(run: SyncRun) -> list[RunEventResult]:
    if run.error_json is None and run.status in {
        SyncRunStatus.QUEUED,
        SyncRunStatus.RUNNING,
    }:
        return []
    error = dict(run.error_json or {})
    if run.status in {SyncRunStatus.CANCELED, SyncRunStatus.SKIPPED}:
        status = "skipped"
    else:
        status = "failed"
    message = str(
        error.get("message")
        or (
            "Der Lauf wurde abgebrochen."
            if status == "skipped"
            else "Der Lauf ist vor der Planerstellung fehlgeschlagen."
        )
    )
    return [
        RunEventResult(
            id=f"run:{run.id}",
            status=status,
            messages=[
                RunResultMessage(
                    code=str(error.get("kind") or "run_failed"),
                    message=message,
                    severity="warning" if status == "skipped" else "error",
                    phase="run",
                    details=error,
                )
            ],
            action_counts=_action_counts(()),
            action_total=0,
        )
    ]
