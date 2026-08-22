from __future__ import annotations

import uuid
from typing import Any, Protocol

from .models import ProviderConnection


class RunDispatcher(Protocol):
    """Narrow queue boundary implemented by the worker package."""

    def enqueue(self, run_id: uuid.UUID) -> None: ...


class NullRunDispatcher:
    """Keeps API-only development usable; recovery can enqueue persisted runs."""

    def enqueue(self, run_id: uuid.UUID) -> None:
        return None


class ConnectionTester(Protocol):
    def test(
        self, connection: ProviderConnection, credentials: dict[str, Any]
    ) -> dict[str, Any]: ...


class ConnectionProbeClient(Protocol):
    """Credential-free API boundary implemented by the probe queue client."""

    def test(
        self, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> dict[str, Any]: ...

    def metadata(
        self, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...
