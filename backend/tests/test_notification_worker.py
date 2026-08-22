from __future__ import annotations

import uuid

import pytest

from app import notification_worker
from app.worker import sync_run_actor


class FakeDispatchRedis:
    def __init__(self):
        self.value: str | None = None
        self.ttl: int | None = None

    def set(self, key, value, *, nx, ex):
        assert key == notification_worker._DELIVERY_DISPATCH_KEY
        assert nx is True
        if self.value is not None:
            return False
        self.value = value
        self.ttl = ex
        return True

    def eval(self, _script, key_count, key, token):
        assert key_count == 1
        assert key == notification_worker._DELIVERY_DISPATCH_KEY
        if self.value != token:
            return 0
        self.value = None
        return 1


class RecordingActor:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.tokens: list[str] = []

    def send(self, token):
        self.tokens.append(token)
        if self.fail:
            raise RuntimeError("broker unavailable")


def test_notification_dispatch_gate_is_token_fenced(settings, monkeypatch):
    redis = FakeDispatchRedis()
    actor = RecordingActor()
    monkeypatch.setattr(notification_worker, "_dispatch_redis", lambda _url: redis)
    monkeypatch.setattr(notification_worker, "deliver_outbox_actor", actor)

    assert notification_worker.enqueue_delivery_once(settings)
    assert not notification_worker.enqueue_delivery_once(settings)
    assert len(actor.tokens) == 1
    token = actor.tokens[0]
    assert redis.value == token
    assert redis.ttl == 60 * 60

    notification_worker._release_delivery_dispatch(settings, "stale-token")
    assert redis.value == token
    notification_worker._release_delivery_dispatch(settings, token)
    assert redis.value is None

    assert notification_worker.enqueue_delivery_once(settings)
    assert len(actor.tokens) == 2


def test_notification_dispatch_gate_is_released_when_broker_send_fails(
    settings, monkeypatch
):
    redis = FakeDispatchRedis()
    actor = RecordingActor(fail=True)
    monkeypatch.setattr(notification_worker, "_dispatch_redis", lambda _url: redis)
    monkeypatch.setattr(notification_worker, "deliver_outbox_actor", actor)

    with pytest.raises(RuntimeError, match="broker unavailable"):
        notification_worker.enqueue_delivery_once(settings)

    assert redis.value is None


def test_sync_and_notification_actors_use_separate_queues():
    assert sync_run_actor is not None
    assert sync_run_actor.queue_name == "sync"
    assert notification_worker.deliver_outbox_actor.queue_name == "notifications"
    assert (
        notification_worker.fanout_run_notifications_actor.queue_name
        == "notifications"
    )
    assert notification_worker.retention_cleanup_actor.queue_name == "notifications"


def test_direct_run_fanout_locks_and_skips_an_already_processed_run(
    settings, monkeypatch
):
    run = type(
        "Run",
        (),
        {"notifications_fanned_out_at": None},
    )()

    class FakeSession:
        def __init__(self):
            self.locked = False
            self.commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalar(self, statement):
            self.locked = statement._for_update_arg is not None
            return run

        def commit(self):
            self.commits += 1

    session = FakeSession()

    class FakeDatabase:
        def session_factory(self):
            return session

        def dispose(self):
            pass

    calls: list[object] = []

    class FakeNotificationService:
        def __init__(self, _db, _settings):
            pass

        def fanout_run(self, value):
            calls.append(value)
            value.notifications_fanned_out_at = object()
            return 3

    dispatches: list[object] = []
    monkeypatch.setattr(notification_worker, "Settings", lambda: settings)
    monkeypatch.setattr(notification_worker, "Database", lambda _settings: FakeDatabase())
    monkeypatch.setattr(notification_worker, "set_worker_context", lambda _db: None)
    monkeypatch.setattr(
        notification_worker, "NotificationService", FakeNotificationService
    )
    monkeypatch.setattr(
        notification_worker,
        "enqueue_delivery_once",
        lambda value: dispatches.append(value) or True,
    )

    actor = notification_worker.fanout_run_notifications_actor.fn
    assert actor(str(uuid.uuid4())) == 3
    assert session.locked
    assert calls == [run]
    assert dispatches == [settings]

    assert actor(str(uuid.uuid4())) == 0
    assert calls == [run]
    assert dispatches == [settings, settings]
