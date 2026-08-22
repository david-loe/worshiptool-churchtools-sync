"""Small scheduler service; due-run creation stays transactional in Postgres."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .sync.ports import DueRunRepository, RunDispatcher

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SchedulerService:
    repository: DueRunRepository
    dispatcher: RunDispatcher
    batch_size: int = 100
    _last_retention_date: date | None = None

    async def tick(self, now: datetime | None = None) -> tuple[str, ...]:
        now = now or datetime.now(timezone.utc)
        run_ids = tuple(await self.repository.create_due_runs(now, self.batch_size))
        for run_id in run_ids:
            try:
                await self.dispatcher.enqueue(run_id)
            except Exception as exc:
                # create_due_runs claimed this dispatch attempt durably before
                # broker I/O. A failure is therefore retried only after the
                # configured redelivery timeout, not on every scheduler tick.
                logger.error(
                    "sync run dispatch failed",
                    extra={"run_id": run_id, "error_type": type(exc).__name__},
                )
                continue
            try:
                await self.repository.mark_dispatched(run_id, now)
            except Exception as exc:
                # The pre-send attempt marker remains durable even if this
                # acknowledgement fails. At-least-once delivery may create one
                # duplicate after the timeout; actor claim is idempotent.
                logger.error(
                    "sync run dispatch acknowledgement failed",
                    extra={"run_id": run_id, "error_type": type(exc).__name__},
                )
        await self._dispatch_maintenance(now)
        return run_ids

    async def serve(self, interval_seconds: float = 15.0) -> None:
        if interval_seconds < 1:
            raise ValueError("scheduler interval must be at least one second")
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # A transient Redis/DB outage must not permanently stop the
                # scheduler container; all operations are idempotent.
                logger.error(
                    "scheduler tick failed",
                    extra={"error_type": type(exc).__name__},
                )
            await asyncio.sleep(interval_seconds)

    async def _dispatch_maintenance(self, now: datetime) -> None:
        try:
            from .notification_worker import (
                enqueue_delivery_once,
                retention_cleanup_actor,
            )

            if await self.repository.notification_work_due(now):
                await asyncio.to_thread(enqueue_delivery_once)
            current_date = now.astimezone(timezone.utc).date()
            if self._last_retention_date != current_date:
                retention_cleanup_actor.send()
                self._last_retention_date = current_date
        except Exception as exc:
            # Run scheduling succeeded transactionally; failed maintenance
            # dispatch is retried on the next tick.
            logger.error(
                "notification maintenance dispatch failed",
                extra={"error_type": type(exc).__name__},
            )


class DramatiqRunDispatcher:
    async def enqueue(self, run_id: str) -> None:
        from .worker import sync_run_actor

        if sync_run_actor is None:
            raise RuntimeError("Dramatiq is not installed")
        sync_run_actor.send(run_id)


def main() -> None:
    from .dramatiq_setup import configure_dramatiq
    from .runtime import runtime_context

    context = runtime_context()
    configure_dramatiq(context.settings)
    asyncio.run(
        SchedulerService(
            context.due_repository,
            DramatiqRunDispatcher(),
        ).serve()
    )


if __name__ == "__main__":
    main()
