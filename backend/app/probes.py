"""Bounded Redis/Dramatiq RPC for provider tests and metadata.

The production API never decrypts provider credentials or opens provider
connections.  It submits identifiers only; a worker-role process resolves the
connection and publishes a short-lived, strictly redacted response.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from enum import Enum
from functools import lru_cache
from typing import Any, Callable, Mapping, Protocol

from redis import Redis
from sqlalchemy import select

from .config import Settings, get_settings
from .database import Database
from .models import ProviderConnection, ProviderType
from .outbox import set_worker_context
from .security import SecretCipher


logger = logging.getLogger(__name__)

PROBE_QUEUE_NAME = "probes"
PROBE_ACTOR_NAME = "provider_probe_actor"
PROBE_RESPONSE_PREFIX = "wt-sync:probe:response:"
PROBE_FLIGHT_PREFIX = "wt-sync:probe:flight:"

_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_OPTION_ITEMS = 10_000
_MAX_TOTAL_OPTION_ITEMS = 2_000
_MAX_METADATA_TEXT_CHARS = 160_000
_IN_FLIGHT_TTL_SECONDS = 60
_RESULT_TTL_SECONDS = 30
_COOLDOWN_SECONDS = 5
_DEFAULT_WAIT_SECONDS = 20.0
_POLL_SECONDS = 0.05
_SAFE_CAPABILITIES = frozenset({"events", "agenda", "songs", "metadata", "services"})
_SAFE_ERROR_CODES = frozenset(
    {
        "provider_probe_queue_unavailable",
        "provider_probe_connection_unavailable",
        "provider_probe_provider_failed",
        "provider_probe_invalid_result",
        "provider_probe_result_too_large",
    }
)

_PUBLISH_IF_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
  redis.call('expire', KEYS[1], ARGV[4])
  return 1
end
return 0
"""


class ProbeOperation(str, Enum):
    TEST = "test"
    METADATA = "metadata"


class ProviderProbeError(RuntimeError):
    """Safe, classified probe failure suitable for an API problem response."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class ProbeRedis(Protocol):
    def set(self, name: str, value: str, **kwargs: Any) -> Any: ...
    def get(self, name: str) -> str | bytes | None: ...
    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...
    def close(self) -> Any: ...


class RedisProviderProbeClient:
    """Synchronous API-side client with singleflight and a bounded wait."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        redis_client: ProbeRedis | None = None,
        enqueue: Callable[[str, str, str, str], None] | None = None,
        wait_seconds: float = _DEFAULT_WAIT_SECONDS,
        poll_seconds: float = _POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 < wait_seconds <= 30:
            raise ValueError("probe wait_seconds must be between 0 and 30")
        if not 0 < poll_seconds <= 1:
            raise ValueError("probe poll_seconds must be between 0 and 1")
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        self.enqueue = enqueue or self._enqueue_actor
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sleep = sleep

    def test(
        self, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> dict[str, Any]:
        return self._request(ProbeOperation.TEST, workspace_id, connection_id)

    def metadata(
        self, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> dict[str, Any]:
        return self._request(ProbeOperation.METADATA, workspace_id, connection_id)

    def close(self) -> None:
        try:
            self.redis.close()
        except Exception:
            logger.warning("provider probe Redis client could not be closed")

    def _request(
        self,
        operation: ProbeOperation,
        workspace_id: uuid.UUID,
        connection_id: uuid.UUID,
    ) -> dict[str, Any]:
        workspace = _canonical_uuid(workspace_id)
        connection = _canonical_uuid(connection_id)
        request_id = uuid.uuid4().hex
        flight_key = _flight_key(workspace, connection, operation)
        try:
            acquired = bool(
                self.redis.set(
                    flight_key,
                    request_id,
                    nx=True,
                    ex=_IN_FLIGHT_TTL_SECONDS,
                )
            )
        except Exception:
            raise ProviderProbeError(
                "provider_probe_redis_unavailable", retry_after=2
            ) from None

        if not acquired:
            existing = self._redis_get(flight_key)
            if not existing or not _REQUEST_ID_RE.fullmatch(existing):
                raise ProviderProbeError(
                    "provider_probe_invalid_response", retry_after=2
                )
            cached = self._redis_get(_response_key(existing))
            if cached is not None:
                return _decode_response(
                    cached, operation, existing, workspace, connection
                )
            # Do not tie up another API worker/thread while the shared request
            # is already waiting on provider I/O.
            raise ProviderProbeError("provider_probe_in_progress", retry_after=2)

        try:
            self.enqueue(workspace, connection, operation.value, request_id)
        except Exception:
            response = _error_response(
                operation, "provider_probe_queue_unavailable"
            )
            try:
                _publish_response(
                    self.redis,
                    workspace,
                    connection,
                    operation,
                    request_id,
                    response,
                )
            except Exception:
                pass
            raise ProviderProbeError(
                "provider_probe_queue_unavailable", retry_after=5
            ) from None

        deadline = self.clock() + self.wait_seconds
        response_key = _response_key(request_id)
        while True:
            raw = self._redis_get(response_key)
            if raw is not None:
                return _decode_response(
                    raw, operation, request_id, workspace, connection
                )
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProviderProbeError(
                    "provider_probe_timeout", retry_after=5
                )
            self.sleep(min(self.poll_seconds, remaining))

    def _enqueue_actor(
        self,
        workspace_id: str,
        connection_id: str,
        operation: str,
        request_id: str,
    ) -> None:
        from .dramatiq_setup import configure_dramatiq

        configure_dramatiq(self.settings)
        from .worker import provider_probe_actor

        if provider_probe_actor is None:
            raise RuntimeError("Dramatiq is unavailable")
        provider_probe_actor.send(
            workspace_id, connection_id, operation, request_id
        )

    def _redis_get(self, key: str) -> str | None:
        try:
            value = self.redis.get(key)
        except Exception:
            raise ProviderProbeError(
                "provider_probe_redis_unavailable", retry_after=2
            ) from None
        if isinstance(value, bytes):
            if len(value) > _MAX_RESPONSE_BYTES:
                raise ProviderProbeError("provider_probe_invalid_response")
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                raise ProviderProbeError("provider_probe_invalid_response") from None
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise ProviderProbeError("provider_probe_invalid_response")
        return value


class DatabaseProviderProbeExecutor:
    """Worker-role executor that is the only component loading credentials."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        tester: Any | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        if tester is None:
            from .runtime import ProviderConnectionTester

            tester = ProviderConnectionTester()
        self.tester = tester

    def execute(
        self,
        workspace_id: str,
        connection_id: str,
        operation: ProbeOperation,
    ) -> dict[str, Any]:
        try:
            workspace_uuid = uuid.UUID(workspace_id)
            connection_uuid = uuid.UUID(connection_id)
        except (TypeError, ValueError):
            return _error_response(
                operation, "provider_probe_connection_unavailable"
            )

        db = self.database.session_factory()
        try:
            set_worker_context(db)
            row = db.scalar(
                select(ProviderConnection).where(
                    ProviderConnection.id == connection_uuid,
                    ProviderConnection.workspace_id == workspace_uuid,
                )
            )
            if row is None:
                return _error_response(
                    operation, "provider_probe_connection_unavailable"
                )
            encrypted = row.credentials_encrypted
            connection = ProviderConnection(
                id=row.id,
                workspace_id=row.workspace_id,
                provider=row.provider,
                name=row.name,
                base_url=row.base_url,
                settings_json=dict(row.settings_json or {}),
            )
            db.commit()
        except Exception:
            db.rollback()
            return _error_response(
                operation, "provider_probe_connection_unavailable"
            )
        finally:
            db.close()

        credentials: dict[str, Any] = {}
        if encrypted:
            try:
                credentials = SecretCipher(self.settings).decrypt_json(
                    encrypted, context=f"connection:{connection.id}"
                )
            except Exception:
                return _error_response(
                    operation, "provider_probe_connection_unavailable"
                )

        if operation is ProbeOperation.TEST:
            try:
                raw = self.tester.test(connection, credentials)
            except Exception:
                raw = {"succeeded": False}
            return _success_response(operation, _safe_test_result(raw))

        try:
            raw_metadata = self.tester.metadata(connection, credentials)
            metadata = _safe_metadata_result(raw_metadata)
        except Exception:
            return _error_response(
                operation, "provider_probe_provider_failed"
            )
        return _success_response(operation, metadata)


def execute_provider_probe_actor(
    workspace_id: str,
    connection_id: str,
    operation: str,
    request_id: str,
    *,
    executor: DatabaseProviderProbeExecutor | None = None,
    redis_client: ProbeRedis | None = None,
) -> None:
    """Validate an actor message before deriving any Redis key or DB query."""

    try:
        workspace = _canonical_uuid(workspace_id)
        connection = _canonical_uuid(connection_id)
        parsed_operation = ProbeOperation(operation)
    except (TypeError, ValueError):
        return
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        return

    runtime_executor = executor or _runtime_probe_executor()
    try:
        response = runtime_executor.execute(
            workspace, connection, parsed_operation
        )
    except Exception as exc:
        logger.error(
            "provider probe execution failed",
            extra={
                "operation": parsed_operation.value,
                "error_type": type(exc).__name__,
            },
        )
        response = _error_response(
            parsed_operation, "provider_probe_provider_failed"
        )
    client = redis_client or _probe_redis(get_settings().redis_url)
    try:
        _publish_response(
            client,
            workspace,
            connection,
            parsed_operation,
            request_id,
            response,
        )
    except Exception:
        # No response data, exception text, or credentials are logged here.
        logger.warning(
            "provider probe response could not be published",
            extra={"operation": parsed_operation.value},
        )


def _safe_test_result(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    succeeded = source.get("succeeded") is True
    identity_source = source.get("identity")
    identity: dict[str, str] = {}
    if succeeded and isinstance(identity_source, Mapping):
        for key in ("id", "email", "account_id"):
            value = identity_source.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                identity[key] = str(value).strip()[:300]
    capabilities_source = source.get("capabilities")
    capabilities: list[str] = []
    if succeeded and isinstance(capabilities_source, (list, tuple)):
        capabilities = sorted(
            {
                value
                for value in capabilities_source
                if isinstance(value, str) and value in _SAFE_CAPABILITIES
            }
        )
    return {
        "succeeded": succeeded,
        "message": (
            "Verbindung erfolgreich."
            if succeeded
            else "Verbindungstest fehlgeschlagen."
        ),
        "identity": identity,
        "capabilities": capabilities,
    }


def _safe_metadata_result(raw: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(raw, Mapping):
        raise ValueError("metadata is not an object")
    result: dict[str, list[dict[str, str]]] = {}
    total_items = 0
    total_text_chars = 0
    for key in ("calendars", "campuses", "song_categories"):
        values = raw.get(key, [])
        if (
            not isinstance(values, (list, tuple))
            or len(values) > _MAX_OPTION_ITEMS
            or total_items + len(values) > _MAX_TOTAL_OPTION_ITEMS
        ):
            raise ValueError("metadata list is invalid")
        options: list[dict[str, str]] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError("metadata option is invalid")
            option_id = value.get("id")
            name = value.get("name")
            if not isinstance(option_id, (str, int)) or not isinstance(name, str):
                raise ValueError("metadata option fields are invalid")
            normalized_id = str(option_id).strip()
            normalized_name = name.strip()
            if (
                not normalized_id
                or len(normalized_id) > 200
                or not normalized_name
                or len(normalized_name) > 300
            ):
                raise ValueError("metadata option fields are invalid")
            total_items += 1
            total_text_chars += len(normalized_id) + len(normalized_name)
            if total_text_chars > _MAX_METADATA_TEXT_CHARS:
                raise ValueError("metadata result is too large")
            options.append({"id": normalized_id, "name": normalized_name})
        result[key] = options
    return result


def _success_response(
    operation: ProbeOperation, data: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "version": 1,
        "operation": operation.value,
        "ok": True,
        "data": dict(data),
    }


def _error_response(operation: ProbeOperation, code: str) -> dict[str, Any]:
    safe_code = code if code in _SAFE_ERROR_CODES else "provider_probe_provider_failed"
    return {
        "version": 1,
        "operation": operation.value,
        "ok": False,
        "error": safe_code,
    }


def _decode_response(
    raw: str,
    operation: ProbeOperation,
    request_id: str,
    workspace_id: str,
    connection_id: str,
) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise ProviderProbeError("provider_probe_invalid_response")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise ProviderProbeError("provider_probe_invalid_response") from None
    if (
        not isinstance(payload, Mapping)
        or payload.get("version") != 1
        or payload.get("operation") != operation.value
        or payload.get("request_id") != request_id
        or payload.get("workspace_id") != workspace_id
        or payload.get("connection_id") != connection_id
        or not isinstance(payload.get("ok"), bool)
    ):
        raise ProviderProbeError("provider_probe_invalid_response")
    if payload["ok"] is False:
        code = payload.get("error")
        if not isinstance(code, str) or code not in _SAFE_ERROR_CODES:
            raise ProviderProbeError("provider_probe_invalid_response")
        raise ProviderProbeError(code, retry_after=5)
    data = payload.get("data")
    if operation is ProbeOperation.TEST:
        return _safe_test_result(data)
    try:
        return _safe_metadata_result(data)
    except ValueError:
        raise ProviderProbeError("provider_probe_invalid_response") from None


def _publish_response(
    redis_client: ProbeRedis,
    workspace_id: str,
    connection_id: str,
    operation: ProbeOperation,
    request_id: str,
    response: Mapping[str, Any],
) -> None:
    bound_response = {
        **response,
        "request_id": request_id,
        "workspace_id": workspace_id,
        "connection_id": connection_id,
    }
    encoded = json.dumps(
        bound_response,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        bounded_error = {
            **_error_response(operation, "provider_probe_result_too_large"),
            "request_id": request_id,
            "workspace_id": workspace_id,
            "connection_id": connection_id,
        }
        encoded = json.dumps(
            bounded_error,
            separators=(",", ":"),
            sort_keys=True,
        )
    redis_client.eval(
        _PUBLISH_IF_OWNER_SCRIPT,
        2,
        _flight_key(workspace_id, connection_id, operation),
        _response_key(request_id),
        request_id,
        encoded,
        _RESULT_TTL_SECONDS,
        _COOLDOWN_SECONDS,
    )


def _canonical_uuid(value: str | uuid.UUID) -> str:
    return str(uuid.UUID(str(value)))


def _response_key(request_id: str) -> str:
    return f"{PROBE_RESPONSE_PREFIX}{request_id}"


def _flight_key(
    workspace_id: str, connection_id: str, operation: ProbeOperation
) -> str:
    return (
        f"{PROBE_FLIGHT_PREFIX}{workspace_id}:{connection_id}:{operation.value}"
    )


@lru_cache(maxsize=1)
def _runtime_probe_executor() -> DatabaseProviderProbeExecutor:
    from .runtime import runtime_context

    context = runtime_context()
    return DatabaseProviderProbeExecutor(context.database, context.settings)


@lru_cache(maxsize=4)
def _probe_redis(redis_url: str) -> Redis:
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
