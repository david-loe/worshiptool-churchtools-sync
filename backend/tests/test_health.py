from __future__ import annotations

import asyncio

import httpx

from app.main import create_app


def test_readiness_requires_database_and_redis(settings, database, monkeypatch):
    monkeypatch.setattr("app.routers.health.check_redis", lambda _settings: None)
    app = create_app(settings, database=database)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            return await client.get("/health/ready")

    ready = asyncio.run(request())

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ok",
        "database": "ok",
        "redis": "ok",
        "version": "0.1.0",
    }

    def unavailable(_settings):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr("app.routers.health.check_redis", unavailable)
    degraded = asyncio.run(request())

    assert degraded.status_code == 503
    assert degraded.json()["status"] == "degraded"
    assert degraded.json()["database"] == "ok"
    assert degraded.json()["redis"] == "error"


def test_readiness_checks_separate_admin_database(settings, database, monkeypatch):
    admin_database = object()
    checked: list[object] = []
    monkeypatch.setattr(
        "app.routers.health.check_database", lambda value: checked.append(value)
    )
    monkeypatch.setattr("app.routers.health.check_redis", lambda _settings: None)
    app = create_app(
        settings,
        database=database,
        admin_database=admin_database,
    )

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            return await client.get("/health/ready")

    response = asyncio.run(request())

    assert response.status_code == 200
    assert checked == [database, admin_database]
