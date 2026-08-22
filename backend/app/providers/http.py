"""Bounded retry and response classification for provider HTTP calls."""

from __future__ import annotations

import asyncio
import email.utils
import json as json_module
import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import httpx

from ..sync.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    IndeterminateWriteError,
    NetworkError,
    NotFoundError,
    ProviderTimeoutError,
    RateLimitError,
    SchemaDriftError,
    SyncError,
)

Sleep = Callable[[float], Awaitable[None]]
_MAX_BODY_BYTES = 8 * 1024 * 1024

# HTTPX emits complete request URLs at INFO. Provider paths can contain
# encrypted connection identifiers, so keep those library logs out of normal
# worker output even when the process root logger is configured at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.25
    max_delay: float = 4.0


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any = None,
    data: Any = None,
    write: bool = False,
    endpoint_label: str | None = None,
    policy: RetryPolicy = RetryPolicy(),
    sleep: Sleep = asyncio.sleep,
) -> Any:
    safe = method.upper() in {"GET", "HEAD", "OPTIONS"} and not write
    public_endpoint = endpoint_label or _safe_path(url)
    for attempt in range(1, policy.attempts + 1):
        retry_delay: float | None = None
        try:
            async with client.stream(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                data=data,
                follow_redirects=False,
            ) as response:
                if response.status_code == 401:
                    raise AuthenticationError(status_code=401, endpoint=public_endpoint)
                if response.status_code == 403:
                    raise AuthorizationError(status_code=403, endpoint=public_endpoint)
                if response.status_code == 404:
                    raise NotFoundError(status_code=404, endpoint=public_endpoint)
                if response.status_code == 409:
                    raise ConflictError(status_code=409, endpoint=public_endpoint)
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    if retry_after is not None and retry_after > policy.max_delay:
                        raise RateLimitError(retry_after, endpoint=public_endpoint)
                    if safe and attempt < policy.attempts:
                        retry_delay = (
                            retry_after
                            if retry_after is not None
                            else _backoff(attempt, policy)
                        )
                    else:
                        raise RateLimitError(retry_after, endpoint=public_endpoint)
                elif response.status_code >= 500:
                    if write:
                        # A 5xx may have happened after the mutation committed.
                        raise IndeterminateWriteError(
                            method=method.upper(),
                            endpoint=public_endpoint,
                            status_code=response.status_code,
                        )
                    if safe and attempt < policy.attempts:
                        retry_delay = _backoff(attempt, policy)
                    else:
                        raise SyncError(
                            "Provider returned a server error",
                            retryable=True,
                            details={
                                "status_code": response.status_code,
                                "endpoint": public_endpoint,
                            },
                        )
                elif response.status_code < 200 or response.status_code >= 300:
                    raise SyncError(
                        "Provider request failed",
                        retryable=False,
                        details={
                            "status_code": response.status_code,
                            "endpoint": public_endpoint,
                        },
                    )
                elif response.status_code == 204 or method.upper() == "HEAD":
                    if write and method.upper() != "DELETE":
                        raise IndeterminateWriteError(
                            method=method.upper(),
                            endpoint=public_endpoint,
                            status_code=response.status_code,
                        )
                    return None
                else:
                    try:
                        content = await _read_bounded_body(
                            response, public_endpoint
                        )
                    except SchemaDriftError as exc:
                        if write:
                            raise IndeterminateWriteError(
                                method=method.upper(),
                                endpoint=public_endpoint,
                                status_code=response.status_code,
                            ) from exc
                        raise
                    if not content:
                        if write and method.upper() != "DELETE":
                            raise IndeterminateWriteError(
                                method=method.upper(),
                                endpoint=public_endpoint,
                                status_code=response.status_code,
                            )
                        return None
                    try:
                        return json_module.loads(content)
                    except (UnicodeDecodeError, ValueError) as exc:
                        if write:
                            raise IndeterminateWriteError(
                                method=method.upper(),
                                endpoint=public_endpoint,
                                status_code=response.status_code,
                            ) from exc
                        raise SchemaDriftError(
                            "Provider returned invalid JSON",
                            endpoint=public_endpoint,
                        ) from exc
        except httpx.TimeoutException as exc:
            if write:
                raise IndeterminateWriteError(method=method.upper(), endpoint=public_endpoint) from exc
            if attempt < policy.attempts:
                await sleep(_backoff(attempt, policy))
                continue
            raise ProviderTimeoutError(method=method.upper(), endpoint=public_endpoint) from exc
        except httpx.TransportError as exc:
            if write:
                raise IndeterminateWriteError(method=method.upper(), endpoint=public_endpoint) from exc
            if attempt < policy.attempts:
                await sleep(_backoff(attempt, policy))
                continue
            raise NetworkError(method=method.upper(), endpoint=public_endpoint) from exc
        if retry_delay is not None:
            # The streaming context has already closed the response before any
            # provider-directed delay is observed.
            await sleep(retry_delay)
            continue
    raise AssertionError("unreachable")


async def _read_bounded_body(
    response: httpx.Response, public_endpoint: str
) -> bytes:
    raw_length = response.headers.get("Content-Length")
    expected_length: int | None = None
    if raw_length is not None:
        if not raw_length.isascii() or not raw_length.isdigit():
            raise SchemaDriftError(
                "Provider returned an invalid Content-Length",
                endpoint=public_endpoint,
            )
        # Avoid feeding an attacker-controlled, arbitrarily long decimal into
        # Python's integer parser. Compare the canonical decimal first and only
        # convert values that are known to fit below the body ceiling.
        canonical_length = raw_length.lstrip("0") or "0"
        maximum_length = str(_MAX_BODY_BYTES)
        if len(canonical_length) > len(maximum_length) or (
            len(canonical_length) == len(maximum_length)
            and canonical_length > maximum_length
        ):
            raise SchemaDriftError(
                "Provider response exceeded the size limit",
                endpoint=public_endpoint,
            )
        expected_length = int(canonical_length)

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > _MAX_BODY_BYTES:
            raise SchemaDriftError(
                "Provider response exceeded the size limit",
                endpoint=public_endpoint,
            )
        body.extend(chunk)

    # Content-Length refers to the encoded representation. It can only be
    # compared with decoded chunks when no content coding was applied.
    if (
        expected_length is not None
        and "Content-Encoding" not in response.headers
        and len(body) != expected_length
    ):
        raise SchemaDriftError(
            "Provider response length did not match Content-Length",
            endpoint=public_endpoint,
        )
    return bytes(body)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


def expect_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaDriftError(f"Provider response field '{label}' is not an object")
    return value


def expect_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaDriftError(f"Provider response field '{label}' is not a list")
    return value


def _backoff(attempt: int, policy: RetryPolicy) -> float:
    maximum = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
    return random.uniform(maximum / 2, maximum)


def _safe_path(url: str) -> str:
    parsed = httpx.URL(url)
    return parsed.path
