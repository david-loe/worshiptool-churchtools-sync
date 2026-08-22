"""Thin Dramatiq entrypoint for the persistence-agnostic sync orchestrator.

Application startup must call :func:`configure_worker` with a factory that
constructs request-scoped SQLAlchemy repositories, provider clients, and the
Redis event lease.  Keeping those dependencies outside this module avoids
import cycles with the API's database/auth packages.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from .probes import (
    PROBE_ACTOR_NAME,
    PROBE_QUEUE_NAME,
    execute_provider_probe_actor,
)
from .sync.engine import SyncOrchestrator
from .sync.ports import EventLeaseManager, ProviderRegistry, RunRepository


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    repository: RunRepository
    providers: ProviderRegistry
    event_leases: EventLeaseManager


_dependency_factory: Callable[[], WorkerDependencies] | None = None


def configure_worker(factory: Callable[[], WorkerDependencies]) -> None:
    global _dependency_factory
    _dependency_factory = factory


async def execute_sync_run_async(run_id: str, dependencies: WorkerDependencies | None = None):
    dependencies = dependencies or _dependencies()
    orchestrator = SyncOrchestrator(
        dependencies.repository,
        dependencies.providers,
        event_leases=dependencies.event_leases,
    )
    return await orchestrator.execute(run_id)


def execute_sync_run(run_id: str, dependencies: WorkerDependencies | None = None):
    """Synchronous actor body; safe for Dramatiq worker threads/processes."""

    return asyncio.run(execute_sync_run_async(run_id, dependencies))


def _dependencies() -> WorkerDependencies:
    if _dependency_factory is None:
        raise RuntimeError("sync worker dependencies have not been configured")
    return _dependency_factory()


try:  # Importing the API remains possible in minimal unit-test environments.
    import dramatiq
except ImportError:  # pragma: no cover - production image installs Dramatiq
    sync_run_actor = None
    provider_probe_actor = None
else:
    from .dramatiq_setup import configure_dramatiq

    configure_dramatiq()

    @dramatiq.actor(
        queue_name="sync",
        max_retries=3,
        min_backoff=5_000,
        max_backoff=60_000,
    )
    def sync_run_actor(run_id: str) -> None:
        status = execute_sync_run(run_id)
        # A None result means another execution owns/completed the run. Do not
        # fan out stale or duplicate notifications.
        if status is not None:
            fanout_run_notifications_actor.send(run_id)

    @dramatiq.actor(
        actor_name=PROBE_ACTOR_NAME,
        queue_name=PROBE_QUEUE_NAME,
        max_retries=0,
        max_age=60_000,
        time_limit=45_000,
    )
    def provider_probe_actor(
        workspace_id: str,
        connection_id: str,
        operation: str,
        request_id: str,
    ) -> None:
        execute_provider_probe_actor(
            workspace_id, connection_id, operation, request_id
        )

    # Import registers delivery/fanout/retention actors in the same lean worker
    # image. Their implementation does not depend on sync repository wiring.
    from .notification_worker import (  # noqa: F401,E402
        deliver_outbox_actor,
        fanout_run_notifications_actor,
        retention_cleanup_actor,
    )

    # Dramatiq imports this module in every worker process.  Configure the
    # concrete SQL/Redis ports after actor registration to avoid import cycles.
    from .runtime import worker_dependencies  # noqa: E402

    configure_worker(worker_dependencies)
