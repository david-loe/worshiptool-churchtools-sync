from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.dependencies import WorkspaceAccess
from app.main import create_app
from app.models import ProviderConnection, ProviderType, User, Workspace, WorkspaceRole
from app.probes import (
    DatabaseProviderProbeExecutor,
    ProbeOperation,
    ProviderProbeError,
    RedisProviderProbeClient,
    execute_provider_probe_actor,
)
from app.problems import ProblemException
from app.routers.connections import (
    get_connection_metadata,
    test_connection as run_connection_test,
)
from app.services import NullRunDispatcher


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def eval(self, script: str, numkeys: int, *values: object) -> int:
        del script
        assert numkeys == 2
        flight_key, response_key, request_id, encoded, _result_ttl, _cooldown = values
        if self.values.get(str(flight_key)) != request_id:
            return 0
        self.values[str(response_key)] = str(encoded)
        return 1

    def close(self) -> None:
        self.closed = True


class AdvancingClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class StubExecutor:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def execute(self, workspace_id: str, connection_id: str, operation):
        del workspace_id, connection_id, operation
        self.calls += 1
        return self.response


def test_probe_client_round_trips_and_reuses_a_singleflight_result(settings) -> None:
    redis = FakeRedis()
    executor = StubExecutor(
        {
            "version": 1,
            "operation": "test",
            "ok": True,
            "data": {
                "succeeded": True,
                "message": "untrusted",
                "identity": {"id": 7, "token": "must-not-leak"},
                "capabilities": ["songs", "unknown"],
            },
        }
    )
    queued: list[tuple[str, str, str, str]] = []

    def enqueue(*args: str) -> None:
        queued.append(args)
        execute_provider_probe_actor(
            *args, executor=executor, redis_client=redis
        )

    client = RedisProviderProbeClient(
        settings, redis_client=redis, enqueue=enqueue
    )
    workspace_id = "00000000-0000-0000-0000-000000000001"
    connection_id = "00000000-0000-0000-0000-000000000002"

    first = client.test(workspace_id, connection_id)
    second = client.test(workspace_id, connection_id)

    assert first == second == {
        "succeeded": True,
        "message": "Verbindung erfolgreich.",
        "identity": {"id": "7"},
        "capabilities": ["songs"],
    }
    assert len(queued) == 1
    assert executor.calls == 1
    assert queued[0][:3] == (workspace_id, connection_id, "test")
    assert len(queued[0][3]) == 32


def test_probe_client_classifies_timeout_queue_and_redis_failures(settings) -> None:
    clock = AdvancingClock()
    timeout_client = RedisProviderProbeClient(
        settings,
        redis_client=FakeRedis(),
        enqueue=lambda *_args: None,
        wait_seconds=0.2,
        poll_seconds=0.1,
        clock=clock,
        sleep=clock.sleep,
    )
    with pytest.raises(ProviderProbeError) as timeout:
        timeout_client.metadata(
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        )
    assert timeout.value.code == "provider_probe_timeout"

    queue_client = RedisProviderProbeClient(
        settings,
        redis_client=FakeRedis(),
        enqueue=lambda *_args: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    with pytest.raises(ProviderProbeError) as queue:
        queue_client.test(
            "00000000-0000-0000-0000-000000000003",
            "00000000-0000-0000-0000-000000000004",
        )
    assert queue.value.code == "provider_probe_queue_unavailable"
    assert "secret" not in str(queue.value)

    class BrokenRedis(FakeRedis):
        def set(self, *args, **kwargs):
            raise ConnectionError("redis-password")

    redis_client = RedisProviderProbeClient(
        settings, redis_client=BrokenRedis(), enqueue=lambda *_args: None
    )
    with pytest.raises(ProviderProbeError) as redis_error:
        redis_client.test(
            "00000000-0000-0000-0000-000000000005",
            "00000000-0000-0000-0000-000000000006",
        )
    assert redis_error.value.code == "provider_probe_redis_unavailable"
    assert "redis-password" not in str(redis_error.value)


def test_probe_follower_returns_immediately_while_singleflight_is_running(settings) -> None:
    redis = FakeRedis()
    workspace_id = "00000000-0000-0000-0000-000000000001"
    connection_id = "00000000-0000-0000-0000-000000000002"
    redis.set(
        f"wt-sync:probe:flight:{workspace_id}:{connection_id}:test",
        "a" * 32,
        ex=60,
    )
    client = RedisProviderProbeClient(
        settings, redis_client=redis, enqueue=lambda *_args: None
    )

    with pytest.raises(ProviderProbeError) as error:
        client.test(workspace_id, connection_id)

    assert error.value.code == "provider_probe_in_progress"
    assert error.value.retry_after == 2


def test_probe_rejects_a_response_bound_to_other_tenant_identifiers(settings) -> None:
    redis = FakeRedis()
    workspace_id = "00000000-0000-0000-0000-000000000001"
    connection_id = "00000000-0000-0000-0000-000000000002"
    request_id = "c" * 32
    redis.set(
        f"wt-sync:probe:flight:{workspace_id}:{connection_id}:test",
        request_id,
        ex=60,
    )
    redis.set(
        f"wt-sync:probe:response:{request_id}",
        json.dumps(
            {
                "version": 1,
                "operation": "test",
                "request_id": request_id,
                "workspace_id": "00000000-0000-0000-0000-000000000099",
                "connection_id": connection_id,
                "ok": True,
                "data": {"succeeded": True},
            }
        ),
        ex=30,
    )
    client = RedisProviderProbeClient(
        settings, redis_client=redis, enqueue=lambda *_args: None
    )

    with pytest.raises(ProviderProbeError) as error:
        client.test(workspace_id, connection_id)

    assert error.value.code == "provider_probe_invalid_response"


def test_worker_executor_loads_credentials_and_redacts_every_result_field(
    database, db, settings
) -> None:
    workspace = Workspace(name="Probe", slug="probe")
    db.add(workspace)
    db.flush()
    connection = ProviderConnection(
        workspace_id=workspace.id,
        provider=ProviderType.WORSHIPTOOLS,
        name="WorshipTools",
        settings_json={},
        credentials_configured=True,
    )
    db.add(connection)
    db.flush()
    from app.security import SecretCipher

    connection.credentials_encrypted = SecretCipher(settings).encrypt_json(
        {
            "email": "probe@example.test",
            "password": "worker-only-secret",
            "account_id": "account",
        },
        context=f"connection:{connection.id}",
    )
    db.commit()

    class Tester:
        def test(self, detached, credentials):
            assert detached.id == connection.id
            assert credentials["password"] == "worker-only-secret"
            return {
                "succeeded": True,
                "message": "worker-only-secret",
                "identity": {
                    "email": credentials["email"],
                    "password": credentials["password"],
                    "token": "hidden",
                },
                "capabilities": ["services", "inject-secret"],
                "raw": credentials,
            }

        def metadata(self, detached, credentials):
            del detached
            return {
                "calendars": [
                    {
                        "id": "calendar-1",
                        "name": "Gottesdienst",
                        "password": credentials["password"],
                    }
                ],
                "campuses": [],
                "song_categories": [],
                "credentials": credentials,
            }

    executor = DatabaseProviderProbeExecutor(
        database, settings, tester=Tester()
    )
    test_result = executor.execute(
        str(workspace.id), str(connection.id), ProbeOperation.TEST
    )
    metadata_result = executor.execute(
        str(workspace.id), str(connection.id), ProbeOperation.METADATA
    )
    serialized = json.dumps([test_result, metadata_result])

    assert test_result["data"] == {
        "succeeded": True,
        "message": "Verbindung erfolgreich.",
        "identity": {"email": "probe@example.test"},
        "capabilities": ["services"],
    }
    assert metadata_result["data"] == {
        "calendars": [{"id": "calendar-1", "name": "Gottesdienst"}],
        "campuses": [],
        "song_categories": [],
    }
    assert "worker-only-secret" not in serialized
    assert "password" not in serialized


def test_actor_rejects_caller_controlled_keys_and_bounds_response_size() -> None:
    redis = FakeRedis()
    executor = StubExecutor({"ok": True})
    execute_provider_probe_actor(
        "not-a-workspace",
        "not-a-connection",
        "test",
        "../../caller-key",
        executor=executor,
        redis_client=redis,
    )
    assert executor.calls == 0
    assert redis.values == {}

    stale_request_id = "a" * 32
    workspace_id = "00000000-0000-0000-0000-000000000001"
    connection_id = "00000000-0000-0000-0000-000000000002"
    redis.set(
        f"wt-sync:probe:flight:{workspace_id}:{connection_id}:test",
        "f" * 32,
        ex=60,
    )
    execute_provider_probe_actor(
        workspace_id,
        connection_id,
        "test",
        stale_request_id,
        executor=StubExecutor(
            {
                "version": 1,
                "operation": "test",
                "ok": True,
                "data": {"succeeded": True},
            }
        ),
        redis_client=redis,
    )
    assert redis.get(f"wt-sync:probe:response:{stale_request_id}") is None

    request_id = "b" * 32
    redis.set(
        f"wt-sync:probe:flight:{workspace_id}:{connection_id}:metadata",
        request_id,
        ex=60,
    )
    huge = StubExecutor(
        {
            "version": 1,
            "operation": "metadata",
            "ok": True,
            "data": {"calendars": [{"id": "x", "name": "s" * 300_000}]},
        }
    )
    execute_provider_probe_actor(
        workspace_id,
        connection_id,
        "metadata",
        request_id,
        executor=huge,
        redis_client=redis,
    )
    response = redis.get(f"wt-sync:probe:response:{request_id}")
    assert response is not None
    assert len(response.encode()) < 1024
    assert "provider_probe_result_too_large" in response
    assert "s" * 100 not in response


def test_production_api_uses_only_the_credential_free_probe_boundary(
    database, db, monkeypatch
) -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql+psycopg://worshipsync_api:pw@db/sync",
        public_base_url="https://sync.example.test",
        application_secret="production-application-secret-material-12345",
        encryption_secret="production-encryption-secret-material-67890",
        require_email_verification=False,
    )

    class ProbeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, object]] = []

        def test(self, workspace_id, connection_id):
            self.calls.append(("test", workspace_id, connection_id))
            return {
                "succeeded": False,
                "message": "Verbindungstest fehlgeschlagen.",
                "identity": {},
                "capabilities": [],
            }

        def metadata(self, workspace_id, connection_id):
            self.calls.append(("metadata", workspace_id, connection_id))
            return {"calendars": [], "campuses": [], "song_categories": []}

        def close(self):
            return None

    class ForbiddenDirectTester:
        def test(self, *_args):
            raise AssertionError("direct tester must not be called")

    probe = ProbeClient()
    app = create_app(
        settings,
        database=database,
        admin_database=database,
        run_dispatcher=NullRunDispatcher(),
        connection_tester=ForbiddenDirectTester(),
        connection_probe_client=probe,
    )
    assert app.state.connection_tester is None
    assert app.state.connection_probe_client is probe

    workspace = Workspace(name="Production Probe", slug="production-probe")
    user = User(
        email="owner@example.test",
        normalized_email="owner@example.test",
        password_hash="unused",
    )
    db.add_all((workspace, user))
    db.flush()
    connection = ProviderConnection(
        workspace_id=workspace.id,
        provider=ProviderType.WORSHIPTOOLS,
        name="WorshipTools",
        settings_json={},
        credentials_encrypted="must-never-be-decrypted-in-api",
        credentials_configured=True,
    )
    db.add(connection)
    db.commit()
    access = WorkspaceAccess(
        workspace=workspace, user=user, role=WorkspaceRole.OWNER
    )
    request = SimpleNamespace(app=app)
    monkeypatch.setattr(
        "app.routers.connections.SecretCipher.decrypt_json",
        lambda *_args, **_kwargs: pytest.fail("API attempted to decrypt credentials"),
    )

    tested = run_connection_test(
        connection.id, request, access, settings, db, None
    )
    metadata = get_connection_metadata(
        connection.id, request, access, settings, db
    )

    assert tested.succeeded is False
    assert metadata.data.calendars == []
    assert probe.calls == [
        ("test", workspace.id, connection.id),
        ("metadata", workspace.id, connection.id),
    ]

    class FailedProbe(ProbeClient):
        def __init__(self, code: str):
            super().__init__()
            self.code = code

        def test(self, workspace_id, connection_id):
            del workspace_id, connection_id
            raise ProviderProbeError(self.code, retry_after=5)

    for code, status in (
        ("provider_probe_queue_unavailable", 503),
        ("provider_probe_timeout", 504),
    ):
        app.state.connection_probe_client = FailedProbe(code)
        with pytest.raises(ProblemException) as error:
            run_connection_test(
                connection.id, request, access, settings, db, None
            )
        assert error.value.code == code
        assert error.value.status == status
        assert error.value.detail.find("worker-only-secret") == -1


def test_production_default_constructs_only_the_queue_probe(
    database, monkeypatch
) -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql+psycopg://worshipsync_api:pw@db/sync",
        public_base_url="https://sync.example.test",
        application_secret="production-application-secret-material-12345",
        encryption_secret="production-encryption-secret-material-67890",
        require_email_verification=False,
    )
    import app.runtime as runtime

    monkeypatch.setattr(
        runtime,
        "ProviderConnectionTester",
        lambda: pytest.fail("production instantiated the direct tester"),
    )

    app = create_app(
        settings,
        database=database,
        admin_database=database,
        run_dispatcher=NullRunDispatcher(),
    )

    assert app.state.connection_tester is None
    assert isinstance(app.state.connection_probe_client, RedisProviderProbeClient)
    app.state.connection_probe_client.close()
