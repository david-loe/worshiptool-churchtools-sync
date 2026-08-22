"""Typed errors shared by providers, planner, and worker code."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    SCHEMA = "schema"
    VALIDATION = "validation"
    INDETERMINATE_WRITE = "indeterminate_write"
    PROVIDER = "provider"
    CONCURRENT_MODIFICATION = "concurrent_modification"


@dataclass(eq=False)
class SyncError(Exception):
    """Base exception whose public representation is safe to persist.

    ``details`` must never contain request headers, credentials, cookies, or
    complete provider response bodies.
    """

    message: str
    kind: ErrorKind = ErrorKind.PROVIDER
    retryable: bool = False
    details: Mapping[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "kind": self.kind.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            value["details"] = dict(self.details)
        return value


class AuthenticationError(SyncError):
    def __init__(self, message: str = "Provider authentication failed", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.AUTHENTICATION, False, kwargs or None)


class AuthorizationError(SyncError):
    def __init__(self, message: str = "Provider authorization failed", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.AUTHORIZATION, False, kwargs or None)


class RateLimitError(SyncError):
    def __init__(self, retry_after: float | None = None, **kwargs: Any) -> None:
        details = {"retry_after": retry_after, **kwargs}
        super().__init__("Provider rate limit reached", ErrorKind.RATE_LIMIT, True, details)
        self.retry_after = retry_after


class NetworkError(SyncError):
    def __init__(self, message: str = "Provider network request failed", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.NETWORK, True, kwargs or None)


class ProviderTimeoutError(SyncError):
    def __init__(self, message: str = "Provider request timed out", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.TIMEOUT, True, kwargs or None)


class SchemaDriftError(SyncError):
    def __init__(self, message: str = "Provider response schema changed", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.SCHEMA, False, kwargs or None)


class NotFoundError(SyncError):
    def __init__(self, message: str = "Provider resource not found", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.NOT_FOUND, False, kwargs or None)


class ConflictError(SyncError):
    def __init__(self, message: str = "Provider rejected a conflicting update", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.CONFLICT, False, kwargs or None)


class IndeterminateWriteError(SyncError):
    """A write may have reached the provider and must not be retried blindly."""

    def __init__(self, message: str = "Provider write outcome is indeterminate", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.INDETERMINATE_WRITE, False, kwargs or None)


class ConcurrentModificationError(SyncError):
    def __init__(self, message: str = "Remote agenda changed after planning", **kwargs: Any) -> None:
        super().__init__(message, ErrorKind.CONCURRENT_MODIFICATION, False, kwargs or None)
