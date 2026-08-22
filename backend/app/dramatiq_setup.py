from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from .config import Settings


_configured_url: str | None = None


def configure_dramatiq(settings: Settings | None = None) -> RedisBroker:
    """Install the shared Redis broker without opening a connection eagerly."""

    global _configured_url
    settings = settings or Settings()
    if _configured_url == settings.redis_url and isinstance(
        dramatiq.get_broker(), RedisBroker
    ):
        return dramatiq.get_broker()
    broker = RedisBroker(url=settings.redis_url)
    dramatiq.set_broker(broker)
    _configured_url = settings.redis_url
    return broker
