"""Redis implementation of the cross-profile target-event lease port."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Protocol


class ThreadSafeRedis(Protocol):
    def set(self, name: str, value: str, *, nx: bool, ex: int) -> Any: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any: ...


_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisEventLeaseManager:
    """Async port backed by redis-py's thread-safe synchronous pool.

    Dramatiq worker threads each use their own ``asyncio.run`` event loop. A
    singleton ``redis.asyncio`` client is loop-bound and unsafe there; the
    synchronous client can be shared and its blocking calls are isolated with
    ``asyncio.to_thread``.
    """

    def __init__(self, redis: ThreadSafeRedis, *, namespace: str = "sync:event-lease") -> None:
        self.redis = redis
        self.namespace = namespace

    async def acquire(
        self, target_connection_id: str, target_event_id: str, owner_token: str, ttl_seconds: int
    ) -> bool:
        key = self._key(target_connection_id, target_event_id)
        return bool(
            await asyncio.to_thread(
                self.redis.set, key, owner_token, nx=True, ex=ttl_seconds
            )
        )

    async def release(self, target_connection_id: str, target_event_id: str, owner_token: str) -> None:
        await asyncio.to_thread(
            self.redis.eval,
            _RELEASE_SCRIPT,
            1,
            self._key(target_connection_id, target_event_id),
            owner_token,
        )

    async def renew(
        self, target_connection_id: str, target_event_id: str, owner_token: str, ttl_seconds: int
    ) -> bool:
        return bool(
            await asyncio.to_thread(
                self.redis.eval,
                _RENEW_SCRIPT,
                1,
                self._key(target_connection_id, target_event_id),
                owner_token,
                str(ttl_seconds),
            )
        )

    def _key(self, connection_id: str, event_id: str) -> str:
        digest = hashlib.sha256(f"{connection_id}\x1f{event_id}".encode("utf-8")).hexdigest()
        return f"{self.namespace}:{digest}"
