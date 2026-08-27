from __future__ import annotations

import uuid
from pathlib import Path
from datetime import datetime, timezone
import json

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Database, _enable_sqlite_foreign_keys
from app.models import (
    Membership,
    ProviderConnection,
    ProviderType,
    RemoteBinding,
    SyncProfile,
    SyncRun,
    SyncTrigger,
    User,
    Workspace,
    WorkspaceRole,
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


def _insert_legacy_profile(
    db: Session,
    workspace: Workspace,
    source: ProviderConnection,
    target: ProviderConnection,
    name: str,
    notification_preferences: dict[str, bool] | None = None,
) -> uuid.UUID:
    profile_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db.execute(
        text(
            "INSERT INTO sync_profiles "
            "(id, workspace_id, source_connection_id, target_connection_id, name, "
            "enabled, revision, match_mode, source_timezone, target_timezone, "
            "lookahead_days, schedule_type, interval_minutes, event_rules, placements, "
            "notification_preferences, create_missing_songs, arrangement_name, "
            "agenda_item_defaults, created_at, updated_at) VALUES "
            "(:id, :workspace_id, :source_id, :target_id, :name, 0, 1, "
            "'exact_time', 'UTC', 'UTC', 28, 'interval', 60, :event_rules, "
            ":placements, :notification_preferences, 1, 'Standard-Arrangement', "
            ":agenda_item_defaults, :created_at, :updated_at)"
        ),
        {
            "id": profile_id.hex,
            "workspace_id": workspace.id.hex,
            "source_id": source.id.hex,
            "target_id": target.id.hex,
            "name": name,
            "event_rules": json.dumps([]),
            "placements": json.dumps([]),
            "notification_preferences": json.dumps(
                notification_preferences or {}
            ),
            "agenda_item_defaults": json.dumps({}),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    return profile_id


def _insert_legacy_notification_preference(
    db: Session,
    workspace: Workspace,
    user: User,
    *,
    email_enabled: bool,
    push_enabled: bool,
    success_notifications: bool,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        text(
            "INSERT INTO notification_preferences "
            "(id, workspace_id, user_id, in_app_enabled, push_enabled, "
            "email_enabled, success_notifications, telegram_enabled, "
            "created_at, updated_at) VALUES "
            "(:id, :workspace_id, :user_id, 1, :push_enabled, "
            ":email_enabled, :success_notifications, 0, :created_at, :updated_at)"
        ),
        {
            "id": uuid.uuid4().hex,
            "workspace_id": workspace.id.hex,
            "user_id": user.id.hex,
            "push_enabled": push_enabled,
            "email_enabled": email_enabled,
            "success_notifications": success_notifications,
            "created_at": now,
            "updated_at": now,
        },
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
                "20260827_0016"
            )
            assert connection.scalar(
                text("SELECT manual_run_cooldown_seconds FROM workspaces")
            ) == 1800
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text("UPDATE workspaces SET manual_run_cooldown_seconds = 600")
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


def test_notification_preference_migration_unions_profiles_and_preserves_user_vetoes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _migration_config(
        tmp_path / "notification-preferences.sqlite3", monkeypatch
    )
    command.upgrade(config, "20260827_0015")
    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            workspace = Workspace(name="Notify", slug="notify-migration")
            enabled_user = User(
                email="enabled@example.org",
                normalized_email="enabled@example.org",
                password_hash="not-used",
            )
            opted_out_user = User(
                email="disabled@example.org",
                normalized_email="disabled@example.org",
                password_hash="not-used",
            )
            db.add_all([workspace, enabled_user, opted_out_user])
            db.flush()
            db.add_all(
                [
                    Membership(
                        workspace_id=workspace.id,
                        user_id=enabled_user.id,
                        role=WorkspaceRole.OWNER,
                    ),
                    Membership(
                        workspace_id=workspace.id,
                        user_id=opted_out_user.id,
                        role=WorkspaceRole.VIEWER,
                    ),
                ]
            )
            source, target = _connections(workspace, "Notify")
            db.add_all([source, target])
            db.flush()
            _insert_legacy_profile(
                db,
                workspace,
                source,
                target,
                "Restrictive",
                {
                    "email": False,
                    "web_push": False,
                    "notify_success": False,
                    "notify_new_songs": False,
                },
            )
            _insert_legacy_profile(
                db,
                workspace,
                source,
                target,
                "Permissive",
                {
                    "email": True,
                    "web_push": True,
                    "notify_success": True,
                    "notify_new_songs": False,
                },
            )
            _insert_legacy_notification_preference(
                db,
                workspace,
                enabled_user,
                email_enabled=True,
                push_enabled=True,
                success_notifications=True,
            )
            _insert_legacy_notification_preference(
                db,
                workspace,
                opted_out_user,
                email_enabled=False,
                push_enabled=False,
                success_notifications=False,
            )
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                text(
                    "INSERT INTO notification_outbox "
                    "(id, workspace_id, channel, recipient, payload_encrypted, "
                    "idempotency_key, status, attempts, created_at, next_attempt_at) "
                    "VALUES (:id, :workspace_id, 'telegram', 'chat', 'payload', "
                    "'legacy-telegram', 'pending', 0, :created_at, :next_attempt_at)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "workspace_id": workspace.id.hex,
                    "created_at": now,
                    "next_attempt_at": now,
                },
            )
            db.commit()
            enabled_id = enabled_user.id.hex
            opted_out_id = opted_out_user.id.hex
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "notification_preferences" not in {
            column["name"] for column in inspector.get_columns("sync_profiles")
        }
        preference_columns = {
            column["name"]
            for column in inspector.get_columns("notification_preferences")
        }
        assert {"failure_notifications", "new_song_notifications"} <= (
            preference_columns
        )
        assert {"in_app_enabled", "telegram_enabled"}.isdisjoint(
            preference_columns
        )
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT user_id, email_enabled, push_enabled, "
                    "success_notifications, failure_notifications, "
                    "new_song_notifications FROM notification_preferences"
                )
            ).mappings()
            preferences = {row["user_id"]: row for row in rows}
            enabled = preferences[enabled_id]
            assert all(
                bool(enabled[field])
                for field in (
                    "email_enabled",
                    "push_enabled",
                    "success_notifications",
                    "failure_notifications",
                )
            )
            assert not bool(enabled["new_song_notifications"])
            opted_out = preferences[opted_out_id]
            assert not bool(opted_out["email_enabled"])
            assert not bool(opted_out["push_enabled"])
            assert not bool(opted_out["success_notifications"])
            assert bool(opted_out["failure_notifications"])
            assert not bool(opted_out["new_song_notifications"])
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM notification_outbox "
                    "WHERE channel = 'telegram'"
                )
            ) == 0
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
            _insert_legacy_profile(
                db, first, source_second, target_first, "Cross tenant"
            )
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
        profile_id = _insert_legacy_profile(
            db, workspace, source, target, "Seed profile"
        )
        run = SyncRun(
            workspace_id=workspace.id,
            profile_id=profile_id,
            config_revision=1,
            trigger=SyncTrigger.MANUAL,
        )
        binding = RemoteBinding(
            workspace_id=workspace.id,
            profile_id=profile_id,
            target_connection_id=target.id,
            target_event_id="seed-event",
            agenda_item_id="seed-item",
            source_key="seed-source",
            placement_id="seed-placement",
        )
        db.add_all([run, binding])
        db.flush()
        seeded = {
            "profile_id": str(profile_id),
            "run_id": str(run.id),
            "binding_id": str(binding.id),
        }
        db.commit()
        # Do not refresh current ORM models against this intentionally old
        # migration revision; newer mapped columns do not exist there yet.
        return seeded


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
    assert "sync_mode" in {
        column["name"] for column in inspector.get_columns("sync_profiles")
    }
    assert inspector.has_table("event_sync_states")
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
            "event_sync_states",
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
