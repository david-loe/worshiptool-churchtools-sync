from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.models import (
    SyncAction,
    SyncActionKind,
    SyncActionStatus,
    SyncRun,
    SyncRunStatus,
    SyncTrigger,
)
from app.run_results import build_run_result
from app.sync.models import (
    ActionKind,
    EventPlan,
    EventPlanStatus,
    PlannedAction,
    SyncPlan,
)


NOW = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)


def _run(*, status: SyncRunStatus, plan: SyncPlan | None = None, error=None, dry_run=False):
    return SyncRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        config_revision=1,
        status=status,
        trigger=SyncTrigger.MANUAL,
        dry_run=dry_run,
        plan_json=(
            {"schema_version": 2, "fingerprint": plan.fingerprint, "plan": plan.as_dict()}
            if plan
            else None
        ),
        error_json=error,
    )


def test_early_failure_produces_one_synthetic_failed_result() -> None:
    run = _run(
        status=SyncRunStatus.FAILED,
        error={"kind": "provider", "message": "Quelle nicht erreichbar"},
    )

    result = build_run_result(run, [])

    assert (result.total, result.failed, result.planned) == (1, 1, 0)
    assert result.events[0].messages[0].code == "provider"


def test_verified_song_is_assigned_to_every_dependent_event() -> None:
    create = PlannedAction(
        id="1" * 32,
        ordinal=0,
        kind=ActionKind.CREATE_SONG,
        payload={
            "source_song_id": "wt-song",
            "name": "Songtitel",
            "author": "Autor",
            "ccli": None,
            "song_resource_key": "song:wt-song",
            "arrangement_resource_key": "arrangement:wt-song",
        },
    )
    event_actions = []
    events = []
    for index in range(2):
        event_id = str(index + 2) * 32
        action = PlannedAction(
            id=str(index + 4) * 32,
            ordinal=index + 1,
            kind=ActionKind.INSERT_ITEM,
            payload={},
            event_plan_id=event_id,
            dependencies=("arrangement:wt-song",),
        )
        event_actions.append(action)
        events.append(
            EventPlan(
                id=event_id,
                source_event_id=f"source-{index}",
                target_event_id=f"target-{index}",
                status=EventPlanStatus.READY,
                source_event_name=f"Service {index}",
                source_event_starts_at=(NOW,),
                target_event_name=f"Gottesdienst {index}",
                target_event_starts_at=NOW,
                actions=(action,),
            )
        )
    plan = SyncPlan(
        run_id=str(uuid.uuid4()),
        profile_id=str(uuid.uuid4()),
        profile_revision=1,
        created_at=NOW,
        preparation_actions=(create,),
        events=tuple(events),
    )
    run = _run(status=SyncRunStatus.SUCCEEDED, plan=plan)
    rows = [
        SyncAction(
            id=uuid.UUID(create.id),
            run_id=run.id,
            kind=SyncActionKind.CREATE_SONG,
            status=SyncActionStatus.VERIFIED,
            ordinal=0,
            payload_json=dict(create.payload),
            fingerprint_json={"song_id": "ct-song"},
        ),
        *[
            SyncAction(
                id=uuid.UUID(action.id),
                run_id=run.id,
                kind=SyncActionKind.INSERT_ITEM,
                status=SyncActionStatus.VERIFIED,
                ordinal=action.ordinal,
                payload_json={},
            )
            for action in event_actions
        ],
    ]

    result = build_run_result(run, rows)

    assert (result.total, result.verified) == (2, 2)
    assert [event.new_songs[0].target_song_id for event in result.events] == [
        "ct-song",
        "ct-song",
    ]
    assert all(event.new_songs[0].ccli is None for event in result.events)


def test_dry_run_ready_event_remains_planned() -> None:
    action = PlannedAction("a" * 32, 0, ActionKind.NOOP, {}, event_plan_id="event")
    plan = SyncPlan(
        run_id=str(uuid.uuid4()),
        profile_id=str(uuid.uuid4()),
        profile_revision=1,
        created_at=NOW,
        preparation_actions=(),
        events=(
            EventPlan(
                id="event",
                source_event_id="source",
                target_event_id="target",
                status=EventPlanStatus.READY,
                actions=(action,),
            ),
        ),
    )
    run = _run(status=SyncRunStatus.SUCCEEDED, plan=plan, dry_run=True)
    row = SyncAction(
        id=uuid.UUID(action.id),
        run_id=run.id,
        kind=SyncActionKind.NOOP,
        status=SyncActionStatus.PLANNED,
        ordinal=0,
        payload_json={},
    )

    result = build_run_result(run, [row])

    assert (result.total, result.planned, result.verified) == (1, 1, 0)
