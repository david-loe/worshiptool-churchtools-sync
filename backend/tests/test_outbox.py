from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import (
    AuditEvent,
    Membership,
    Notification,
    NotificationOutbox,
    NotificationPreference,
    NotificationSeverity,
    OutboxStatus,
    ProviderConnection,
    ProviderType,
    PushSubscription,
    SyncAction,
    SyncActionKind,
    SyncActionStatus,
    SyncProfile,
    SyncRun,
    SyncRunStatus,
    SyncTrigger,
    User,
    Workspace,
    WorkspaceRole,
)
from app.outbox import (
    DeliveryError,
    NotificationService,
    OutboxConsumer,
    RetentionService,
    enqueue_email,
)
from app.routers.auth import register, request_recovery
from app.schemas import RecoveryRequest, RegisterRequest
from app.security import SecretCipher, hash_password
from app.sync.models import (
    ActionKind,
    EventPlan,
    EventPlanStatus,
    PlannedAction,
    SyncPlan,
)


class RecordingSender:
    def __init__(self):
        self.calls = []

    def send(self, recipient, payload):
        self.calls.append((recipient, payload))


class FailingSender:
    def __init__(self, *, permanent: bool = False):
        self.permanent = permanent
        self.calls = 0

    def send(self, recipient, payload):
        self.calls += 1
        raise DeliveryError("test_delivery_failure", permanent=self.permanent)


def _tenant_graph(db):
    user = User(
        email="notify@example.org",
        normalized_email="notify@example.org",
        password_hash=hash_password("correct horse battery staple"),
        email_verified_at=datetime.now(timezone.utc),
    )
    workspace = Workspace(name="Notify", slug=f"notify-{uuid.uuid4().hex[:8]}")
    db.add_all([user, workspace])
    db.flush()
    db.add(
        Membership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
    )
    source = ProviderConnection(
        workspace_id=workspace.id,
        provider=ProviderType.WORSHIPTOOLS,
        name="WT",
    )
    target = ProviderConnection(
        workspace_id=workspace.id,
        provider=ProviderType.CHURCHTOOLS,
        name="CT",
        base_url="https://test.church.tools",
    )
    db.add_all([source, target])
    db.flush()
    profile = SyncProfile(
        workspace_id=workspace.id,
        source_connection_id=source.id,
        target_connection_id=target.id,
        name="Main",
        song_category_id=1,
    )
    db.add(profile)
    db.flush()
    return user, workspace, profile


def test_outbox_delivers_encrypted_email_exactly_once(database, db, settings):
    item = enqueue_email(
        db,
        settings,
        recipient="person@example.org",
        subject="Test",
        text="Sensitive body",
        idempotency_key="email:test:one",
    )
    db.commit()
    assert "Sensitive body" not in item.payload_encrypted
    sender = RecordingSender()
    consumer = OutboxConsumer(database, settings, email_sender=sender)

    result = consumer.process_batch()
    second = consumer.process_batch()

    assert (result.claimed, result.delivered) == (1, 1)
    assert second.claimed == 0
    assert sender.calls == [
        (
            "person@example.org",
            {
                "_message_id": str(item.id),
                "html": None,
                "subject": "Test",
                "text": "Sensitive body",
            },
        )
    ]
    db.expire_all()
    stored = db.get(NotificationOutbox, item.id)
    assert stored is not None and stored.status == OutboxStatus.DELIVERED
    assert stored.attempts == 1


def test_outbox_retries_then_dead_letters_without_storing_exception_text(
    database, db, settings
):
    settings = settings.model_copy(update={"outbox_max_attempts": 2})
    item = enqueue_email(
        db,
        settings,
        recipient="person@example.org",
        subject="Retry",
        text="Body",
        idempotency_key="email:test:retry",
    )
    db.commit()
    sender = FailingSender()
    consumer = OutboxConsumer(database, settings, email_sender=sender)
    now = datetime.now(timezone.utc)

    first = consumer.process_batch(now=now)
    second = consumer.process_batch(now=now + timedelta(days=1))

    assert first.retried == 1
    assert second.dead == 1
    db.expire_all()
    stored = db.get(NotificationOutbox, item.id)
    assert stored is not None and stored.status == OutboxStatus.DEAD
    assert stored.last_error_code == "test_delivery_failure"
    assert stored.attempts == 2


def test_outbox_stale_claim_cannot_overwrite_new_owner(database, db, settings):
    item = enqueue_email(
        db,
        settings,
        recipient="person@example.org",
        subject="Fenced",
        text="Body",
        idempotency_key="email:test:fenced",
    )
    db.commit()
    sender = RecordingSender()
    consumer = OutboxConsumer(database, settings, email_sender=sender)
    now = datetime.now(timezone.utc)

    first = consumer._claim_one(now)
    assert first is not None
    second = consumer._claim_one(
        now + timedelta(seconds=settings.outbox_lease_seconds + 1)
    )
    assert second is not None
    assert first[0] == second[0] == item.id
    assert first[1] != second[1]

    assert consumer._deliver(item.id, first[1], now) == OutboxStatus.FAILED
    assert sender.calls == []

    outcome = consumer._fail(
        item.id,
        first[1],
        now,
        "stale_worker_failure",
        True,
    )
    assert outcome == OutboxStatus.FAILED
    db.expire_all()
    stored = db.get(NotificationOutbox, item.id)
    assert stored is not None
    assert stored.status == OutboxStatus.PROCESSING
    assert stored.claim_token == second[1]
    assert stored.last_error_code is None


def test_outbox_heartbeat_and_completion_are_claim_token_fenced(
    database, db, settings, monkeypatch
):
    item = enqueue_email(
        db,
        settings,
        recipient="person@example.org",
        subject="Heartbeat",
        text="Body",
        idempotency_key="email:test:heartbeat",
    )
    db.commit()
    sender = RecordingSender()
    consumer = OutboxConsumer(database, settings, email_sender=sender)
    now = datetime.now(timezone.utc)

    first = consumer._claim_one(now)
    assert first is not None
    renewal_time = now + timedelta(seconds=10)
    assert consumer._renew_claim(item.id, first[1], now=renewal_time)

    takeover_time = renewal_time + timedelta(
        seconds=settings.outbox_lease_seconds + 1
    )
    second = consumer._claim_one(takeover_time)
    assert second is not None and second[1] != first[1]
    db.expire_all()
    before_stale_renewal = db.get(NotificationOutbox, item.id).next_attempt_at
    assert not consumer._renew_claim(
        item.id,
        first[1],
        now=takeover_time + timedelta(seconds=1),
    )
    db.expire_all()
    stored = db.get(NotificationOutbox, item.id)
    assert stored.claim_token == second[1]
    assert stored.next_attempt_at == before_stale_renewal

    @contextmanager
    def heartbeat_reported_lost(_item_id, _claim_token):
        lost = threading.Event()
        lost.set()
        yield lost

    monkeypatch.setattr(consumer, "_claim_heartbeat", heartbeat_reported_lost)
    outcome = consumer._deliver(item.id, second[1], takeover_time)

    assert outcome == OutboxStatus.DELIVERED
    assert len(sender.calls) == 1
    db.expire_all()
    delivered = db.get(NotificationOutbox, item.id)
    assert delivered.status == OutboxStatus.DELIVERED
    assert delivered.claim_token is None


def test_enqueue_outbox_duplicate_uses_savepoint_and_preserves_outer_work(
    db, settings, monkeypatch
):
    first = enqueue_email(
        db,
        settings,
        recipient="person@example.org",
        subject="First",
        text="Body",
        idempotency_key="email:test:savepoint-race",
    )
    db.commit()

    unrelated = Workspace(name="Survives", slug=f"survives-{uuid.uuid4().hex[:8]}")
    db.add(unrelated)
    db.flush()
    original_scalar = db.scalar
    calls = 0

    def miss_preflight_once(statement, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Deterministically model a concurrent insert committed after the
            # optimistic existence check but before our nested INSERT.
            return None
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db, "scalar", miss_preflight_once)
    duplicate = enqueue_email(
        db,
        settings,
        recipient="person@example.org",
        subject="Duplicate",
        text="Different body",
        idempotency_key="email:test:savepoint-race",
    )
    db.commit()

    assert duplicate.id == first.id
    assert db.get(Workspace, unrelated.id) is not None
    assert (
        db.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(
                NotificationOutbox.idempotency_key
                == "email:test:savepoint-race"
            )
        )
        == 1
    )


def test_run_fanout_respects_preferences_and_push_payload_shape(
    database, db, settings
):
    user, workspace, profile = _tenant_graph(db)
    db.add(
        NotificationPreference(
            workspace_id=workspace.id,
            user_id=user.id,
            email_enabled=False,
            push_enabled=True,
            success_notifications=False,
        )
    )
    subscription = PushSubscription(
        workspace_id=workspace.id,
        user_id=user.id,
        endpoint_hash="endpoint-hash",
        subscription_encrypted="pending",
        device_name="Test Browser",
    )
    db.add(subscription)
    db.flush()
    subscription.subscription_encrypted = SecretCipher(settings).encrypt_json(
        {
            "endpoint": "https://push.example/subscription",
            "keys": {"p256dh": "public-key", "auth": "auth-secret"},
        },
        context=f"push-subscription:{subscription.id}",
    )
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=1,
        status=SyncRunStatus.FAILED,
        trigger=SyncTrigger.SCHEDULED,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()

    created = NotificationService(db, settings).fanout_run(run)
    db.commit()

    assert created == 1
    assert db.scalar(select(func.count()).select_from(Notification)) == 1
    rows = db.scalars(select(NotificationOutbox)).all()
    assert [row.channel for row in rows] == ["push"]
    push_sender = RecordingSender()
    result = OutboxConsumer(database, settings, push_sender=push_sender).process_batch()
    assert result.delivered == 1
    _, payload = push_sender.calls[0]
    assert set(payload) == {"title", "body", "data"}
    assert payload["data"]["url"] == (
        f"/runs/{run.id}?workspace={workspace.id}"
    )

    NotificationService(db, settings).create_for_user(
        workspace_id=workspace.id,
        user=user,
        severity=NotificationSeverity.INFO,
        category="new_songs",
        title="Neuer Song",
        body="Ein neuer Song wurde angelegt.",
        run_id=run.id,
        data={"event_plan_id": "event-plan-1"},
        deduplication_key="push-event-link",
        channel_policy={"email": False, "web_push": True},
    )
    db.commit()
    event_sender = RecordingSender()
    event_result = OutboxConsumer(
        database, settings, push_sender=event_sender
    ).process_batch()
    assert event_result.delivered == 1
    assert event_sender.calls[0][1]["data"]["url"] == (
        f"/runs/{run.id}?workspace={workspace.id}&event=event-plan-1"
    )


def test_profile_channel_policy_suppresses_external_delivery_but_keeps_in_app(
    db, settings
):
    user, workspace, profile = _tenant_graph(db)
    profile.notification_preferences = {
        "in_app": True,
        "email": False,
        "web_push": False,
        "telegram": False,
        "notify_success": False,
        "notify_new_songs": True,
    }
    db.add(
        NotificationPreference(
            workspace_id=workspace.id,
            user_id=user.id,
            email_enabled=True,
            push_enabled=True,
        )
    )
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=1,
        status=SyncRunStatus.FAILED,
        trigger=SyncTrigger.SCHEDULED,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()

    assert NotificationService(db, settings).fanout_run(run) == 1

    assert db.scalar(select(func.count()).select_from(Notification)) == 1
    assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_fanout_reports_verified_new_songs_separately(db, settings):
    user, workspace, profile = _tenant_graph(db)
    profile.notification_preferences = {
        "in_app": True,
        "email": False,
        "web_push": False,
        "telegram": False,
        "notify_success": False,
        "notify_new_songs": True,
    }
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=1,
        status=SyncRunStatus.PARTIAL,
        trigger=SyncTrigger.SCHEDULED,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    db.add(
        SyncAction(
            run_id=run.id,
            kind=SyncActionKind.CREATE_SONG,
            status=SyncActionStatus.VERIFIED,
            ordinal=0,
        )
    )
    db.flush()

    assert NotificationService(db, settings).fanout_run(run) == 2

    categories = db.scalars(
        select(Notification.category).order_by(Notification.category)
    ).all()
    assert categories == ["new_songs", "sync_run"]


def test_fanout_bundles_verified_new_songs_per_event(db, settings):
    _user, workspace, profile = _tenant_graph(db)
    profile.notification_preferences = {
        "in_app": True,
        "email": False,
        "web_push": False,
        "telegram": False,
        "notify_success": False,
        "notify_new_songs": True,
    }
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=1,
        status=SyncRunStatus.PARTIAL,
        trigger=SyncTrigger.SCHEDULED,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    create = PlannedAction(
        "1" * 32,
        0,
        ActionKind.CREATE_SONG,
        {
            "source_song_id": "source-song",
            "name": "Neuer Song",
            "author": "Autor",
            "ccli": None,
            "song_resource_key": "song:source-song",
            "arrangement_resource_key": "arrangement:source-song",
        },
    )
    event_actions = (
        PlannedAction(
            "4" * 32,
            1,
            ActionKind.INSERT_ITEM,
            {},
            event_plan_id="2" * 32,
            dependencies=("arrangement:source-song",),
        ),
        PlannedAction(
            "5" * 32,
            2,
            ActionKind.INSERT_ITEM,
            {},
            event_plan_id="3" * 32,
            dependencies=("arrangement:source-song",),
        ),
    )
    plan = SyncPlan(
        run_id=str(run.id),
        profile_id=str(profile.id),
        profile_revision=1,
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        preparation_actions=(create,),
        events=tuple(
            EventPlan(
                id=str(index + 2) * 32,
                source_event_id=f"source-{index}",
                target_event_id=f"target-{index}",
                status=EventPlanStatus.READY,
                source_event_name=f"Service {index}",
                source_event_starts_at=(
                    datetime(2026, 9, 6 + index, 8, tzinfo=timezone.utc),
                ),
                target_event_name=f"Gottesdienst {index}",
                target_event_starts_at=datetime(
                    2026, 9, 6 + index, 8, tzinfo=timezone.utc
                ),
                actions=(event_actions[index],),
            )
            for index in range(2)
        ),
    )
    run.plan_json = {
        "schema_version": 2,
        "fingerprint": plan.fingerprint,
        "plan": plan.as_dict(),
    }
    db.add_all(
        [
            SyncAction(
                id=uuid.UUID(create.id),
                run_id=run.id,
                kind=SyncActionKind.CREATE_SONG,
                status=SyncActionStatus.VERIFIED,
                ordinal=0,
                payload_json=dict(create.payload),
                fingerprint_json={"song_id": "churchtools-song"},
            ),
            *[
                SyncAction(
                    id=uuid.UUID(action.id),
                    run_id=run.id,
                    event_id=f"target-{index}",
                    kind=SyncActionKind.INSERT_ITEM,
                    status=SyncActionStatus.VERIFIED,
                    ordinal=action.ordinal,
                    payload_json={},
                )
                for index, action in enumerate(event_actions)
            ],
        ]
    )
    db.flush()

    assert NotificationService(db, settings).fanout_run(run) == 3

    song_notifications = db.scalars(
        select(Notification)
        .where(Notification.category == "new_songs")
        .order_by(Notification.title)
    ).all()
    assert len(song_notifications) == 2
    assert {item.data_json["event_plan_id"] for item in song_notifications} == {
        "2" * 32,
        "3" * 32,
    }
    assert all(item.data_json["songs_created"] == 1 for item in song_notifications)


def test_registration_and_recovery_enqueue_transactional_emails(db, settings):
    settings = settings.model_copy(
        update={
            "require_email_verification": True,
            "expose_development_tokens": False,
            "public_base_url": "https://sync.example.org",
        }
    )
    response = register(
        RegisterRequest(
            email="verify@example.org",
            password="correct horse battery staple",
            workspace_name="Verify",
        ),
        settings,
        db,
    )
    assert response.development_verification_token is None
    verification = db.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key.like("verify-email:%")
        )
    )
    assert verification is not None
    verify_payload = SecretCipher(settings).decrypt_json(
        verification.payload_encrypted, context=f"outbox:{verification.id}"
    )
    assert "https://sync.example.org/email-bestaetigen?token=" in verify_payload["text"]

    recovery = request_recovery(
        RecoveryRequest(email="verify@example.org"), settings, db
    )
    assert recovery.development_recovery_token is None
    reset = db.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key.like("password-recovery:%")
        )
    )
    assert reset is not None
    reset_payload = SecretCipher(settings).decrypt_json(
        reset.payload_encrypted, context=f"outbox:{reset.id}"
    )
    assert (
        "https://sync.example.org/passwort-zuruecksetzen?token="
        in reset_payload["text"]
    )


def test_retention_removes_history_but_keeps_pending_delivery(database, db, settings):
    user, workspace, profile = _tenant_graph(db)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=settings.retention_days + 1)
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=1,
        status=SyncRunStatus.FAILED,
        trigger=SyncTrigger.SCHEDULED,
        created_at=old,
        finished_at=old,
    )
    db.add(run)
    db.flush()
    db.add(
        SyncAction(
            run_id=run.id,
            kind=SyncActionKind.NOOP,
            ordinal=0,
            planned_at=old,
        )
    )
    db.add(
        Notification(
            workspace_id=workspace.id,
            user_id=user.id,
            severity=NotificationSeverity.ERROR,
            category="old",
            title="Old",
            body="Old",
            created_at=old,
        )
    )
    db.add(
        AuditEvent(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="old",
            created_at=old,
        )
    )
    pending = enqueue_email(
        db,
        settings,
        recipient=user.email,
        subject="Pending",
        text="Pending",
        workspace_id=workspace.id,
        idempotency_key="pending:must-survive",
    )
    pending.created_at = old
    db.commit()
    run_id = run.id
    pending_id = pending.id

    counts = RetentionService(database, settings).cleanup(now=now)

    assert counts["sync_runs"] == 1
    db.expire_all()
    assert db.get(SyncRun, run_id) is None
    assert db.scalar(select(func.count()).select_from(SyncAction)) == 0
    assert db.scalar(select(func.count()).select_from(Notification)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
    assert db.get(NotificationOutbox, pending_id) is not None
