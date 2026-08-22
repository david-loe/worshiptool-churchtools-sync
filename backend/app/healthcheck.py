"""Low-overhead dependency probe shared by API and background containers."""

from __future__ import annotations

from redis import Redis
from sqlalchemy import text

from .config import Settings
from .database import Database


def check_database(database: Database) -> None:
    with database.session_factory() as db:
        db.execute(text("SELECT 1"))


def check_redis(settings: Settings) -> None:
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        health_check_interval=30,
    )
    try:
        if client.ping() is not True:
            raise RuntimeError("Redis ping failed")
    finally:
        client.close()


def main() -> None:
    settings = Settings()
    database = Database(settings)
    try:
        check_database(database)
        check_redis(settings)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
