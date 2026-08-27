from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import (
    EventSyncState,
    NotificationOutbox,
    OutboxStatus,
    ProviderConnection,
    ProviderType,
    RemoteBinding,
    SyncProfile,
    SyncRun,
    SyncRunStatus,
    SyncTrigger,
    Workspace,
)
from app.runtime import (
    DatabaseProviderRegistry,
    ProviderConnectionTester,
    SqlDueRunRepository,
    SqlRunRepository,
)
from app.security import SecretCipher
from app.schemas import ProfileOut
from app.scheduling import next_schedule_after
from app.sync.errors import AuthenticationError, ConcurrentModificationError
from app.sync.leases import RedisEventLeaseManager
from app.sync.models import (
    ActionExecution,
    ActionKind,
    ActionStatus,
    EventPlan,
    EventPlanStatus,
    Ownership,
    PlannedAction,
    RunStatus,
    SyncPlan,
)


NOW = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)


def _seed_connections(db, settings):
    workspace = Workspace(name="Runtime", slug="runtime")
    db.add(workspace)
    db.flush()
    source = ProviderConnection(
        workspace_id=workspace.id,
        provider=ProviderType.WORSHIPTOOLS,
        name="WT",
        settings_json={},
        credentials_configured=True,
    )
    target = ProviderConnection(
        workspace_id=workspace.id,
        provider=ProviderType.CHURCHTOOLS,
        name="CT",
        base_url="https://runtime.church.tools",
        settings_json={},
        credentials_configured=True,
    )
    db.add_all((source, target))
    db.flush()
    cipher = SecretCipher(settings)
    source.credentials_encrypted = cipher.encrypt_json(
        {
            "email": "runtime@example.test",
            "password": "secret",
            "account_id": "account",
        },
        context=f"connection:{source.id}",
    )
    target.credentials_encrypted = cipher.encrypt_json(
        {"token": "Login private-token"},
        context=f"connection:{target.id}",
    )
    return workspace, source, target


def _profile(db, workspace, source, target, name: str, **values):
    profile = SyncProfile(
        workspace_id=workspace.id,
        source_connection_id=source.id,
        target_connection_id=target.id,
        name=name,
        enabled=values.pop("enabled", True),
        song_category_id=7,
        placements=[
            {
                "id": "main",
                "anchor": {"item_type": "header", "title": "Lobpreis"},
                "relation": "after",
                "song_start": 0,
                "song_end": None,
            }
        ],
        **values,
    )
    db.add(profile)
    db.flush()
    return profile


def _run(db, profile, *, status, trigger=SyncTrigger.MANUAL, **values):
    run = SyncRun(
        workspace_id=profile.workspace_id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=status,
        trigger=trigger,
        dry_run=False,
        **values,
    )
    db.add(run)
    db.flush()
    return run


def test_due_repository_redelivers_queued_and_expired_runs_after_dispatch_crash(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    queued_profile = _profile(db, workspace, source, target, "Queued")
    expired_profile = _profile(db, workspace, source, target, "Expired")
    fresh_profile = _profile(db, workspace, source, target, "Fresh")
    due_profile = _profile(db, workspace, source, target, "Due")
    queued = _run(
        db,
        queued_profile,
        status=SyncRunStatus.QUEUED,
        created_at=NOW - timedelta(minutes=5),
    )
    expired = _run(
        db,
        expired_profile,
        status=SyncRunStatus.RUNNING,
        created_at=NOW - timedelta(minutes=4),
        lease_owner="dead-worker",
        lease_expires_at=NOW - timedelta(seconds=1),
    )
    fresh = _run(
        db,
        fresh_profile,
        status=SyncRunStatus.RUNNING,
        created_at=NOW - timedelta(minutes=3),
        lease_owner="healthy-worker",
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    db.commit()

    repository = SqlDueRunRepository(database)
    first = set(asyncio.run(repository.create_due_runs(NOW, 100)))

    assert str(queued.id) in first
    assert str(expired.id) in first
    assert str(fresh.id) not in first
    db.expire_all()
    recovered = db.get(SyncRun, expired.id)
    assert recovered.status == SyncRunStatus.QUEUED
    assert recovered.lease_owner is None
    assert recovered.lease_expires_at is None
    scheduled = db.scalars(
        select(SyncRun).where(
            SyncRun.profile_id == due_profile.id,
            SyncRun.trigger == SyncTrigger.SCHEDULED,
        )
    ).all()
    assert len(scheduled) == 1
    scheduled_id = str(scheduled[0].id)
    assert scheduled_id in first
    db.commit()

    # Simulate the scheduler dying after commit but before all broker sends.
    # The durable pre-send claim prevents a duplicate message on every 15s
    # tick, while preserving bounded redelivery after the timeout.
    second = set(asyncio.run(repository.create_due_runs(NOW + timedelta(seconds=15), 100)))
    assert scheduled_id not in second
    assert str(queued.id) not in second
    assert str(expired.id) not in second
    count = db.scalar(
        select(func.count())
        .select_from(SyncRun)
        .where(
            SyncRun.profile_id == due_profile.id,
            SyncRun.trigger == SyncTrigger.SCHEDULED,
        )
    )
    assert count == 1
    db.commit()

    after_timeout = set(
        asyncio.run(repository.create_due_runs(NOW + timedelta(minutes=6), 100))
    )
    assert scheduled_id in after_timeout
    assert str(queued.id) in after_timeout
    assert str(expired.id) in after_timeout


def test_due_profile_query_cannot_be_starved_by_many_future_cron_profiles(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    for index in range(25):
        _profile(
            db,
            workspace,
            source,
            target,
            f"Future {index:02d}",
            schedule_type="cron",
            interval_minutes=None,
            cron_expression="0 9 * * 0",
            next_scheduled_at=NOW + timedelta(days=7),
        )
    due = _profile(
        db,
        workspace,
        source,
        target,
        "Due behind future profiles",
        next_scheduled_at=NOW - timedelta(seconds=1),
    )
    db.commit()

    run_ids = asyncio.run(SqlDueRunRepository(database).create_due_runs(NOW, 1))

    assert len(run_ids) == 1
    created = db.get(SyncRun, uuid.UUID(run_ids[0]))
    assert created.profile_id == due.id
    db.expire_all()
    next_scheduled_at = db.get(SyncProfile, due.id).next_scheduled_at
    assert next_scheduled_at is not None
    if next_scheduled_at.tzinfo is None:
        next_scheduled_at = next_scheduled_at.replace(tzinfo=timezone.utc)
    assert next_scheduled_at > NOW


def test_notification_work_due_tracks_fanout_due_retries_and_expired_claims(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    profile = _profile(db, workspace, source, target, "Notifications")
    run = _run(
        db,
        profile,
        status=SyncRunStatus.FAILED,
        finished_at=NOW - timedelta(seconds=1),
    )
    db.commit()
    repository = SqlDueRunRepository(database)

    assert repository._notification_work_due(NOW)

    run.notifications_fanned_out_at = NOW
    item = NotificationOutbox(
        channel="email",
        recipient="person@example.org",
        payload_encrypted="encrypted-test-payload",
        idempotency_key="notification-work-due",
        status=OutboxStatus.PENDING,
        next_attempt_at=NOW + timedelta(seconds=1),
    )
    db.add(item)
    db.commit()
    assert not repository._notification_work_due(NOW)

    item.next_attempt_at = NOW
    db.commit()
    assert repository._notification_work_due(NOW)

    item.status = OutboxStatus.PROCESSING
    item.claim_token = "current-owner"
    item.next_attempt_at = NOW + timedelta(seconds=1)
    db.commit()
    assert not repository._notification_work_due(NOW)

    item.next_attempt_at = NOW
    db.commit()
    assert repository._notification_work_due(NOW)


def test_sql_run_repository_claim_plan_recovery_and_owner_checked_finish(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    profile = _profile(db, workspace, source, target, "Plan")
    run = _run(db, profile, status=SyncRunStatus.QUEUED)
    db.commit()
    repository = SqlRunRepository(database)

    specification = asyncio.run(repository.claim(str(run.id), "worker-a", 300))
    assert specification is not None
    assert specification.workspace_id == str(workspace.id)
    assert specification.profile.placements[0].multiple_anchor_policy.value == "fail"
    assert asyncio.run(repository.claim(str(run.id), "worker-b", 300)) is None

    plan = SyncPlan(
        run_id=str(run.id),
        profile_id=str(profile.id),
        profile_revision=profile.revision,
        created_at=NOW,
        preparation_actions=(),
        events=(),
    )
    asyncio.run(repository.persist_plan(str(run.id), plan, "worker-a"))
    db.expire_all()
    assert db.get(SyncRun, run.id).plan_json["schema_version"] == 2
    recovered = asyncio.run(repository.load_plan(str(run.id)))
    assert recovered is not None
    assert recovered.fingerprint == plan.fingerprint

    assert asyncio.run(
        repository.finish(
            str(run.id), RunStatus.SUCCEEDED, worker_id="stale-worker"
        )
    ) is False
    db.expire_all()
    assert db.get(SyncRun, run.id).status == SyncRunStatus.RUNNING

    assert asyncio.run(
        repository.finish(str(run.id), RunStatus.SUCCEEDED, worker_id="worker-a")
    ) is True
    db.expire_all()
    finished = db.get(SyncRun, run.id)
    assert finished.status == SyncRunStatus.SUCCEEDED
    assert finished.lease_owner is None


def test_persisted_legacy_event_filters_are_visible_and_canonical_for_worker(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    profile = _profile(
        db,
        workspace,
        source,
        target,
        "Legacy selectors",
        event_rules=[
            {
                "name_contains": "Service",
                "calendar_ids": ["canonical-calendar"],
                "calendar_id": "stale-hidden-calendar",
                "campus_ids": ["canonical-campus"],
                "campus_name": "Stale hidden campus",
            },
            {
                "calendar_id": 42,
                "campus_name": "Legacy campus without a stable ID",
            },
        ],
    )
    run = _run(db, profile, status=SyncRunStatus.QUEUED)
    db.commit()

    response_rules = ProfileOut.model_validate(profile).model_dump()["event_rules"]
    assert response_rules == [
        {
            "name_contains": "Service",
            "name_regex": None,
            "campus_ids": ["canonical-campus"],
            "calendar_ids": ["canonical-calendar"],
        },
        {
            "name_contains": None,
            "name_regex": None,
            "campus_ids": [],
            "calendar_ids": ["42"],
        },
    ]

    specification = asyncio.run(
        SqlRunRepository(database).claim(str(run.id), "canonical-worker", 300)
    )
    assert specification is not None
    selectors = specification.profile.selectors
    assert selectors[0].calendar_ids == ("canonical-calendar",)
    assert selectors[0].campus_ids == ("canonical-campus",)
    assert selectors[1].calendar_ids == ("42",)
    assert selectors[1].campus_ids == ()
    assert not hasattr(selectors[0], "calendar_id")
    assert not hasattr(selectors[0], "campus_name")


def test_sql_fencing_rejects_every_stale_executor_mutation_after_takeover(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    profile = _profile(db, workspace, source, target, "Fencing")
    run = _run(db, profile, status=SyncRunStatus.QUEUED)
    db.commit()
    repository = SqlRunRepository(database)
    assert asyncio.run(repository.claim(str(run.id), "owner-a", 300)) is not None
    plan = SyncPlan(
        run_id=str(run.id),
        profile_id=str(profile.id),
        profile_revision=profile.revision,
        created_at=NOW,
        preparation_actions=(
            PlannedAction("1" * 32, 0, ActionKind.NOOP, {}),
        ),
        events=(),
    )
    asyncio.run(repository.persist_plan(str(run.id), plan, "owner-a"))

    db.expire_all()
    leased = db.get(SyncRun, run.id)
    leased.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert asyncio.run(repository.renew_lease(str(run.id), "owner-a", 300)) is False
    assert asyncio.run(repository.claim(str(run.id), "owner-b", 300)) is not None

    with pytest.raises(ConcurrentModificationError):
        asyncio.run(repository.persist_plan(str(run.id), plan, "owner-a"))
    with pytest.raises(ConcurrentModificationError):
        asyncio.run(
            repository.action_started(
                str(run.id), plan.preparation_actions[0], "owner-a"
            )
        )
    with pytest.raises(ConcurrentModificationError):
        asyncio.run(
            repository.action_finished(
                str(run.id),
                ActionExecution("1" * 32, ActionStatus.SKIPPED),
                "owner-a",
            )
        )
    with pytest.raises(ConcurrentModificationError):
        asyncio.run(
            repository.bind_ownership(
                str(run.id),
                Ownership(
                    str(profile.id), "event", "item", "main:0:song", "main"
                ),
                "owner-a",
            )
        )
    with pytest.raises(ConcurrentModificationError):
        asyncio.run(
            repository.unbind_ownership(
                str(run.id),
                str(profile.id),
                "event",
                "item",
                "owner-a",
            )
        )
    assert asyncio.run(
        repository.finish(
            str(run.id), RunStatus.FAILED, worker_id="owner-a"
        )
    ) is False
    assert asyncio.run(
        repository.finish(
            str(run.id), RunStatus.SUCCEEDED, worker_id="owner-b"
        )
    ) is True
    db.expire_all()
    assert db.get(SyncRun, run.id).status == SyncRunStatus.SUCCEEDED


def test_ownership_lookup_exposes_other_profile_on_same_target_connection(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    first = _profile(db, workspace, source, target, "First")
    second = _profile(db, workspace, source, target, "Second")
    db.add(
        RemoteBinding(
            workspace_id=workspace.id,
            profile_id=second.id,
            target_connection_id=target.id,
            target_event_id="event-1",
            agenda_item_id="item-1",
            source_key="main:0:song",
            placement_id="main",
        )
    )
    db.commit()

    rows = asyncio.run(
        SqlRunRepository(database).ownerships(str(first.id), "event-1")
    )

    assert len(rows) == 1
    assert rows[0].profile_id == str(second.id)


def test_run_repository_persists_and_loads_verified_event_sync_state(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    profile_row = _profile(db, workspace, source, target, "Event state")
    run = _run(db, profile_row, status=SyncRunStatus.QUEUED)
    run_id = str(run.id)
    profile_id = str(profile_row.id)
    db.commit()
    repository = SqlRunRepository(database)

    specification = asyncio.run(repository.claim(run_id, "state-owner", 300))
    assert specification is not None
    event = EventPlan(
        id="event-plan",
        source_event_id="wt-event",
        target_event_id="ct-event",
        status=EventPlanStatus.READY,
        source_fingerprint="a" * 64,
        config_fingerprint="b" * 64,
    )
    asyncio.run(repository.record_event_synced(run_id, event, "state-owner"))

    states = asyncio.run(
        repository.event_sync_states(profile_id, (("wt-event", "ct-event"),))
    )
    assert states[("wt-event", "ct-event")].source_fingerprint == "a" * 64
    assert states[("wt-event", "ct-event")].config_fingerprint == "b" * 64
    db.expire_all()
    persisted = db.scalar(
        select(EventSyncState).where(EventSyncState.profile_id == profile_row.id)
    )
    assert persisted is not None
    assert persisted.workspace_id == workspace.id


def test_provider_registry_requires_complete_worshiptools_credentials_and_strips_ct_prefix(
    database, db, settings
) -> None:
    workspace, source, target = _seed_connections(db, settings)
    db.commit()
    registry = DatabaseProviderRegistry(database, settings)

    churchtools = registry.target(str(workspace.id), str(target.id), "Europe/Berlin")
    assert churchtools._headers["Authorization"] == "Login private-token"
    asyncio.run(churchtools.aclose())

    source.credentials_encrypted = SecretCipher(settings).encrypt_json(
        {"email": "runtime@example.test", "password": "secret"},
        context=f"connection:{source.id}",
    )
    db.commit()
    with pytest.raises(AuthenticationError, match="account_id"):
        registry.source(str(workspace.id), str(source.id), "Europe/Berlin")


def test_provider_registry_preserves_significant_worshiptools_password_whitespace(
    database, db, settings
) -> None:
    workspace, source, _target = _seed_connections(db, settings)
    source.credentials_encrypted = SecretCipher(settings).encrypt_json(
        {
            "email": "runtime@example.test",
            "password": "  significant password  ",
            "account_id": "tenant",
        },
        context=f"connection:{source.id}",
    )
    db.commit()

    client = DatabaseProviderRegistry(database, settings).source(
        str(workspace.id), str(source.id), "Europe/Berlin"
    )
    assert client._password == "  significant password  "
    asyncio.run(client.aclose())


def test_connection_tester_returns_a_clear_missing_field_error() -> None:
    connection = ProviderConnection(
        provider=ProviderType.WORSHIPTOOLS,
        name="Incomplete",
        settings_json={},
    )

    result = ProviderConnectionTester().test(
        connection, {"email": "person@example.test"}
    )

    assert result["succeeded"] is False
    assert "Passwort" in result["message"]
    assert "Account-ID" in result["message"]


def test_schedule_calculation_is_utc_and_target_timezone_aware() -> None:
    interval = next_schedule_after(
        schedule_type="interval",
        interval_minutes=60,
        cron_expression=None,
        timezone_name="Europe/Berlin",
        after=NOW,
    )
    cron = next_schedule_after(
        schedule_type="cron",
        interval_minutes=None,
        cron_expression="0 9 * * *",
        timezone_name="Europe/Berlin",
        after=datetime(2026, 3, 28, 9, 0, tzinfo=timezone.utc),
    )

    assert interval == NOW + timedelta(hours=1)
    assert cron == datetime(2026, 3, 29, 7, 0, tzinfo=timezone.utc)


class _ThreadSafeFakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = threading.Lock()

    def set(self, name: str, value: str, *, nx: bool, ex: int):
        with self.lock:
            if nx and name in self.values:
                return False
            self.values[name] = value
            return True

    def eval(self, script: str, numkeys: int, *values: str):
        key, owner = values[:2]
        with self.lock:
            if self.values.get(key) != owner:
                return 0
            if "del" in script:
                self.values.pop(key, None)
            return 1


def test_redis_lease_manager_is_safe_across_dramatiq_threads_and_event_loops() -> None:
    manager = RedisEventLeaseManager(_ThreadSafeFakeRedis())

    def acquire(owner: str) -> tuple[str, bool]:
        return owner, asyncio.run(manager.acquire("connection", "event", owner, 60))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = dict(executor.map(acquire, ("owner-a", "owner-b")))
    assert sorted(results.values()) == [False, True]
    winner = next(owner for owner, acquired in results.items() if acquired)
    loser = next(owner for owner, acquired in results.items() if not acquired)
    assert asyncio.run(manager.renew("connection", "event", loser, 60)) is False
    asyncio.run(manager.release("connection", "event", loser))
    assert asyncio.run(manager.renew("connection", "event", winner, 60)) is True
