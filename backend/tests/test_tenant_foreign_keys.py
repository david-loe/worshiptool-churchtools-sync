from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Database, _enable_sqlite_foreign_keys
from app.models import (
    ProviderConnection,
    ProviderType,
    RemoteBinding,
    SyncProfile,
    SyncRun,
    SyncTrigger,
    Workspace,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_database_uses_runtime_override_to_select_sqlite_pragmas(settings) -> None:
    database = Database(
        settings,
        database_url="postgresql+psycopg://runtime:secret@localhost/worshipsync",
    )
    try:
        assert not event.contains(
            database.engine,
            "connect",
            _enable_sqlite_foreign_keys,
        )
    finally:
        database.dispose()


def _connections(
    workspace: Workspace, suffix: str
) -> tuple[ProviderConnection, ProviderConnection]:
    return (
        ProviderConnection(
            workspace_id=workspace.id,
            provider=ProviderType.WORSHIPTOOLS,
            name=f"Source {suffix}",
        ),
        ProviderConnection(
            workspace_id=workspace.id,
            provider=ProviderType.CHURCHTOOLS,
            name=f"Target {suffix}",
            base_url=f"https://{suffix.casefold()}.church.tools",
        ),
    )


def _profile(
    workspace: Workspace,
    source: ProviderConnection,
    target: ProviderConnection,
    name: str,
) -> SyncProfile:
    return SyncProfile(
        workspace_id=workspace.id,
        source_connection_id=source.id,
        target_connection_id=target.id,
        name=name,
    )


def _expect_integrity_error(db: Session, row: object) -> None:
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_model_constraints_reject_cross_workspace_sync_graph(database, db) -> None:
    # SQLite deliberately requires opt-in foreign-key enforcement. Production
    # PostgreSQL always enforces these constraints; enabling it here makes the
    # lightweight model test exercise the same tenant boundary.
    with database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1

    first = Workspace(name="First", slug=f"first-{uuid.uuid4().hex[:8]}")
    second = Workspace(name="Second", slug=f"second-{uuid.uuid4().hex[:8]}")
    db.add_all([first, second])
    db.flush()
    source_first, target_first = _connections(first, "First")
    source_second, target_second = _connections(second, "Second")
    db.add_all([source_first, target_first, source_second, target_second])
    db.commit()

    _expect_integrity_error(
        db,
        _profile(first, source_second, target_first, "Foreign source"),
    )
    _expect_integrity_error(
        db,
        _profile(first, source_first, target_second, "Foreign target"),
    )

    profile_first = _profile(first, source_first, target_first, "First profile")
    profile_second = _profile(second, source_second, target_second, "Second profile")
    db.add_all([profile_first, profile_second])
    db.commit()

    _expect_integrity_error(
        db,
        SyncRun(
            workspace_id=second.id,
            profile_id=profile_first.id,
            config_revision=1,
            trigger=SyncTrigger.MANUAL,
        ),
    )
    _expect_integrity_error(
        db,
        RemoteBinding(
            workspace_id=first.id,
            profile_id=profile_second.id,
            target_connection_id=target_first.id,
            target_event_id="event-profile",
            agenda_item_id="item-profile",
            source_key="source-profile",
            placement_id="placement-profile",
        ),
    )
    _expect_integrity_error(
        db,
        RemoteBinding(
            workspace_id=first.id,
            profile_id=profile_first.id,
            target_connection_id=target_second.id,
            target_event_id="event-target",
            agenda_item_id="item-target",
            source_key="source-target",
            placement_id="placement-target",
        ),
    )

    valid_run = SyncRun(
        workspace_id=first.id,
        profile_id=profile_first.id,
        config_revision=1,
        trigger=SyncTrigger.MANUAL,
    )
    valid_binding = RemoteBinding(
        workspace_id=first.id,
        profile_id=profile_first.id,
        target_connection_id=target_first.id,
        target_event_id="event-valid",
        agenda_item_id="item-valid",
        source_key="source-valid",
        placement_id="placement-valid",
    )
    db.add_all([valid_run, valid_binding])
    db.commit()


def test_fresh_sqlite_migration_installs_and_reverses_tenant_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _migration_config(
        tmp_path / "tenant-fks.sqlite3", monkeypatch
    )

    command.upgrade(config, "20260822_0008")
    engine = create_engine(database_url)
    try:
        seeded = _seed_valid_graph(engine)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        _assert_migrated_constraints(engine)
        _assert_seed_preserved(engine, seeded)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260822_0011"
            )
    finally:
        engine.dispose()

    command.downgrade(config, "20260822_0008")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        _assert_seed_preserved(engine, seeded)
        assert _foreign_key(inspector, "sync_runs", ("profile_id",)) == (
            "sync_profiles",
            ("id",),
        )
        assert _foreign_key(
            inspector, "sync_runs", ("workspace_id", "profile_id")
        ) is None
    finally:
        engine.dispose()

    # A second upgrade catches downgrade artifacts and makes the test database
    # finish at the same schema revision as a fresh production installation.
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        _assert_migrated_constraints(engine)
        _assert_seed_preserved(engine, seeded)
    finally:
        engine.dispose()


def test_migration_refuses_existing_cross_workspace_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _migration_config(
        tmp_path / "tenant-fks-dirty.sqlite3", monkeypatch
    )
    command.upgrade(config, "20260822_0008")
    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            first = Workspace(name="First", slug=f"first-{uuid.uuid4().hex[:8]}")
            second = Workspace(name="Second", slug=f"second-{uuid.uuid4().hex[:8]}")
            db.add_all([first, second])
            db.flush()
            source_first, target_first = _connections(first, "First")
            source_second, _target_second = _connections(second, "Second")
            db.add_all([source_first, target_first, source_second, _target_second])
            db.flush()
            # Revision 0008 has only the legacy ID-only FK, so this is valid in
            # the old schema and represents the corruption 0009 must not hide.
            db.add(_profile(first, source_second, target_first, "Cross tenant"))
            db.commit()
    finally:
        engine.dispose()

    with pytest.raises(
        RuntimeError,
        match=r"sync_profiles\.source_connection_id=1",
    ):
        command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260822_0008"
            )
    finally:
        engine.dispose()


def _migration_config(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Config]:
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("WT_SYNC_ENVIRONMENT", "test")
    monkeypatch.setenv("WT_SYNC_DATABASE_URL", database_url)
    monkeypatch.setenv("WT_SYNC_DATABASE_OWNER_URL", database_url)
    return database_url, Config(str(BACKEND_ROOT / "alembic.ini"))


def _seed_valid_graph(engine) -> dict[str, str]:
    with Session(engine) as db:
        workspace = Workspace(name="Seed", slug=f"seed-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()
        source, target = _connections(workspace, "Seed")
        db.add_all([source, target])
        db.flush()
        profile = _profile(workspace, source, target, "Seed profile")
        db.add(profile)
        db.flush()
        run = SyncRun(
            workspace_id=workspace.id,
            profile_id=profile.id,
            config_revision=1,
            trigger=SyncTrigger.MANUAL,
        )
        binding = RemoteBinding(
            workspace_id=workspace.id,
            profile_id=profile.id,
            target_connection_id=target.id,
            target_event_id="seed-event",
            agenda_item_id="seed-item",
            source_key="seed-source",
            placement_id="seed-placement",
        )
        db.add_all([run, binding])
        db.commit()
        return {
            "profile_id": str(profile.id),
            "run_id": str(run.id),
            "binding_id": str(binding.id),
        }


def _assert_seed_preserved(engine, seeded: dict[str, str]) -> None:
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT COUNT(*) FROM sync_profiles WHERE id = :id"),
            {"id": seeded["profile_id"].replace("-", "")},
        ) == 1
        assert connection.scalar(
            text("SELECT COUNT(*) FROM sync_runs WHERE id = :id"),
            {"id": seeded["run_id"].replace("-", "")},
        ) == 1
        assert connection.scalar(
            text("SELECT COUNT(*) FROM remote_bindings WHERE id = :id"),
            {"id": seeded["binding_id"].replace("-", "")},
        ) == 1

def _assert_migrated_constraints(engine) -> None:
    inspector = inspect(engine)
    provider_uniques = {
        (constraint["name"], tuple(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints("provider_connections")
    }
    profile_uniques = {
        (constraint["name"], tuple(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints("sync_profiles")
    }
    assert (
        "uq_provider_connections_workspace_id_id",
        ("workspace_id", "id"),
    ) in provider_uniques
    assert (
        "uq_sync_profiles_workspace_id_id",
        ("workspace_id", "id"),
    ) in profile_uniques

    expected = (
        (
            "sync_profiles",
            ("workspace_id", "source_connection_id"),
            "provider_connections",
            ("workspace_id", "id"),
        ),
        (
            "sync_profiles",
            ("workspace_id", "target_connection_id"),
            "provider_connections",
            ("workspace_id", "id"),
        ),
        (
            "sync_runs",
            ("workspace_id", "profile_id"),
            "sync_profiles",
            ("workspace_id", "id"),
        ),
        (
            "remote_bindings",
            ("workspace_id", "profile_id"),
            "sync_profiles",
            ("workspace_id", "id"),
        ),
        (
            "remote_bindings",
            ("workspace_id", "target_connection_id"),
            "provider_connections",
            ("workspace_id", "id"),
        ),
    )
    for table, constrained, referred_table, referred in expected:
        assert _foreign_key(inspector, table, constrained) == (
            referred_table,
            referred,
        )


def _foreign_key(inspector, table: str, constrained: tuple[str, ...]):
    for foreign_key in inspector.get_foreign_keys(table):
        if tuple(foreign_key["constrained_columns"]) == constrained:
            return (
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
            )
    return None
