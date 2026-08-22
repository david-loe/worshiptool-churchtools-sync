from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request
from redis import Redis
from redis.exceptions import RedisError

from .config import Settings
from .problems import ProblemException


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int


class RateLimitBackendUnavailable(Exception):
    pass


class RateLimiter(Protocol):
    def check(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult: ...


class InMemoryFixedWindowRateLimiter:
    """Thread-safe test/development fallback; never used across prod replicas."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[int, int]] = {}

    def check(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        now = int(self._clock())
        window = now // window_seconds
        bucket_key = f"{key}:{window}"
        with self._lock:
            count, _ = self._buckets.get(bucket_key, (0, window))
            count += 1
            self._buckets[bucket_key] = (count, window)
            # Bound process memory even under random-key abuse.
            if len(self._buckets) > 10_000:
                minimum = window - 1
                self._buckets = {
                    item_key: value
                    for item_key, value in self._buckets.items()
                    if value[1] >= minimum
                }
        retry_after = max(1, window_seconds - (now % window_seconds))
        return RateLimitResult(count <= limit, retry_after)


class RedisFixedWindowRateLimiter:
    _SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""

    def __init__(self, redis: Redis):
        self._redis = redis

    def check(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        now = int(time.time())
        window = now // window_seconds
        try:
            current = self._redis.eval(
                self._SCRIPT,
                1,
                f"wt-sync:rate:{key}:{window}",
                window_seconds + 1,
            )
        except RedisError as exc:
            raise RateLimitBackendUnavailable from exc
        retry_after = max(1, window_seconds - (now % window_seconds))
        return RateLimitResult(int(current) <= limit, retry_after)


@dataclass(frozen=True)
class AuthRatePolicy:
    window_seconds: int
    ip_limit: int
    identity_limit: int


AUTH_RATE_POLICIES = {
    "register": AuthRatePolicy(60 * 60, 10, 3),
    "login": AuthRatePolicy(15 * 60, 60, 10),
    "recovery": AuthRatePolicy(60 * 60, 20, 3),
    "verification": AuthRatePolicy(60 * 60, 30, 10),
    "totp": AuthRatePolicy(15 * 60, 30, 10),
    # Invitations are authenticated, but they can still be abused as an SMTP
    # relay. The recipient bucket prevents repeatedly targeting one address;
    # the IP bucket also bounds spray attempts across arbitrary recipients.
    "invite": AuthRatePolicy(60 * 60, 60, 3),
}


_limiter_lock = threading.Lock()
_limiters: dict[tuple[str, str], RateLimiter] = {}


def default_rate_limiter(settings: Settings) -> RateLimiter:
    backend = "redis" if settings.environment == "production" else "memory"
    key = (backend, settings.redis_url)
    with _limiter_lock:
        limiter = _limiters.get(key)
        if limiter is None:
            if backend == "redis":
                limiter = RedisFixedWindowRateLimiter(
                    Redis.from_url(
                        settings.redis_url,
                        decode_responses=True,
                        socket_connect_timeout=1.0,
                        socket_timeout=1.0,
                        max_connections=20,
                        health_check_interval=30,
                    )
                )
            else:
                limiter = InMemoryFixedWindowRateLimiter()
            _limiters[key] = limiter
        return limiter


def _safe_key(settings: Settings, scope: str, value: str) -> str:
    digest = hmac.new(
        settings.application_secret.get_secret_value().encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{scope}:{digest}"


def enforce_auth_rate_limit(
    request: Request | None,
    settings: Settings,
    policy_name: str,
    *,
    identity: str,
    include_ip: bool = True,
) -> None:
    # Direct domain tests and internal calls have no transport/IP. HTTP calls
    # always provide Request and exercise both IP and identity buckets.
    if request is None:
        return
    policy = AUTH_RATE_POLICIES[policy_name]
    limiter = getattr(request.app.state, "rate_limiter", None) or default_rate_limiter(
        settings
    )
    ip = request.client.host if request.client else "unknown"
    checks = []
    if include_ip:
        checks.append((_safe_key(settings, f"{policy_name}:ip", ip), policy.ip_limit))
    checks.append(
        (
            _safe_key(settings, f"{policy_name}:identity", identity.casefold()),
            policy.identity_limit,
        )
    )
    for key, limit in checks:
        try:
            result = limiter.check(
                key, limit=limit, window_seconds=policy.window_seconds
            )
        except RateLimitBackendUnavailable as exc:
            # Production is fail-closed: bypassing limits during a Redis outage
            # would turn an infrastructure incident into an auth attack window.
            if settings.environment == "production":
                raise ProblemException(
                    503,
                    "Anfrageschutz vorübergehend nicht verfügbar",
                    "Bitte versuche es in wenigen Sekunden erneut.",
                    "rate_limiter_unavailable",
                    headers={"Retry-After": "5"},
                ) from exc
            # Tests/development fall back locally and remain usable without Redis.
            result = default_rate_limiter(
                settings.model_copy(update={"environment": "test"})
            ).check(key, limit=limit, window_seconds=policy.window_seconds)
        if not result.allowed:
            raise ProblemException(
                429,
                "Zu viele Versuche",
                "Bitte warte, bevor du es erneut versuchst.",
                "rate_limit_exceeded",
                headers={"Retry-After": str(result.retry_after)},
            )
