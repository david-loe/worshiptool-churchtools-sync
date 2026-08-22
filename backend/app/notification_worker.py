from __future__ import annotations

import logging
import uuid
from functools import lru_cache

import dramatiq
from redis import Redis
from sqlalchemy import select

from .config import Settings
from .database import Database
from .dramatiq_setup import configure_dramatiq
from .models import SyncRun
from .outbox import (
    NotificationService,
    OutboxConsumer,
    RetentionService,
    set_worker_context,
)


configure_dramatiq()

logger = logging.getLogger(__name__)

_DELIVERY_DISPATCH_KEY = "worship-sync:notification-delivery-dispatch"
_RELEASE_DISPATCH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@lru_cache(maxsize=8)
def _dispatch_redis(redis_url: str) -> Redis:
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )


def _release_delivery_dispatch(settings: Settings, token: str) -> None:
    _dispatch_redis(settings.redis_url).eval(
        _RELEASE_DISPATCH_SCRIPT,
        1,
        _DELIVERY_DISPATCH_KEY,
        token,
    )


def enqueue_delivery_once(settings: Settings | None = None) -> bool:
    """Queue at most one notification drain signal per hour.

    The actor releases the token and immediately chains a successor when its
    bounded item budget was exhausted. A crashed/down worker leaves one durable
    message instead of thousands; the TTL only guards against a lost message.
    """

    settings = settings or Settings()
    token = uuid.uuid4().hex
    redis = _dispatch_redis(settings.redis_url)
    acquired = redis.set(
        _DELIVERY_DISPATCH_KEY,
        token,
        nx=True,
        ex=60 * 60,
    )
    if not acquired:
        return False
    try:
        deliver_outbox_actor.send(token)
    except Exception:
        _release_delivery_dispatch(settings, token)
        raise
    return True


@dramatiq.actor(
    queue_name="notifications",
    max_retries=3,
    min_backoff=5_000,
    max_backoff=60_000,
)
def deliver_outbox_actor(
    dispatch_token: str | None = None, max_batches: int = 1
) -> dict[str, int]:
    settings = Settings()
    database = Database(settings)
    claimed = delivered = retried = dead = 0
    needs_followup = False
    try:
        # Run-result fanout is idempotent and makes notification recovery
        # independent from the exact point at which a sync worker crashed.
        with database.session_factory() as db:
            set_worker_context(db)
            NotificationService(db, settings).fanout_pending_runs(
                limit=settings.outbox_batch_size
            )
        consumer = OutboxConsumer(database, settings)
        for _ in range(max(1, min(max_batches, 100))):
            result = consumer.process_batch()
            claimed += result.claimed
            delivered += result.delivered
            retried += result.retried
            dead += result.dead
            needs_followup = result.claimed >= settings.outbox_batch_size
            if result.claimed < settings.outbox_batch_size:
                break
    finally:
        database.dispose()
        if dispatch_token:
            try:
                _release_delivery_dispatch(settings, dispatch_token)
            except Exception as exc:
                logger.error(
                    "notification dispatch token release failed",
                    extra={"error_type": type(exc).__name__},
                )
    if needs_followup:
        enqueue_delivery_once(settings)
    return {
        "claimed": claimed,
        "delivered": delivered,
        "retried": retried,
        "dead": dead,
    }


@dramatiq.actor(
    queue_name="notifications",
    max_retries=3,
    min_backoff=5_000,
    max_backoff=60_000,
)
def fanout_run_notifications_actor(run_id: str) -> int:
    settings = Settings()
    database = Database(settings)
    try:
        with database.session_factory() as db:
            set_worker_context(db)
            run = db.scalar(
                select(SyncRun)
                .where(SyncRun.id == uuid.UUID(run_id))
                .with_for_update()
            )
            if run is None:
                return 0
            # The scheduler recovery path locks the same row with SKIP LOCKED.
            # Serializing the direct post-run actor here prevents both paths
            # from racing on Notification.deduplication_key inserts.
            created = (
                0
                if run.notifications_fanned_out_at is not None
                else NotificationService(db, settings).fanout_run(run)
            )
            db.commit()
        enqueue_delivery_once(settings)
        return created
    finally:
        database.dispose()


@dramatiq.actor(queue_name="notifications", max_retries=1)
def retention_cleanup_actor() -> dict[str, int]:
    settings = Settings()
    database = Database(settings)
    try:
        return RetentionService(database, settings).cleanup()
    finally:
        database.dispose()
