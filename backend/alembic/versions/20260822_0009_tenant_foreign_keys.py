"""Fence tenant-owned references with their workspace identifier.

Revision ID: 20260822_0009
Revises: 20260822_0008

The previous single-column foreign keys guaranteed that referenced rows existed,
but did not guarantee that parent and child belonged to the same workspace.
Composite foreign keys make that invariant atomic at the database boundary.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260822_0009"
down_revision = "20260822_0008"
branch_labels = None
depends_on = None


PROVIDER_WORKSPACE_UNIQUE = "uq_provider_connections_workspace_id_id"
PROFILE_WORKSPACE_UNIQUE = "uq_sync_profiles_workspace_id_id"

PROFILE_SOURCE_FK = "fk_sync_profiles_workspace_source_connection"
PROFILE_TARGET_FK = "fk_sync_profiles_workspace_target_connection"
RUN_PROFILE_FK = "fk_sync_runs_workspace_profile"
BINDING_PROFILE_FK = "fk_remote_bindings_workspace_profile"
BINDING_TARGET_FK = "fk_remote_bindings_workspace_target_connection"

OLD_PROFILE_SOURCE_FK = "fk_sync_profiles_source_connection_id_provider_connections"
OLD_PROFILE_TARGET_FK = "fk_sync_profiles_target_connection_id_provider_connections"
OLD_RUN_PROFILE_FK = "fk_sync_runs_profile_id_sync_profiles"
OLD_BINDING_PROFILE_FK = "fk_remote_bindings_profile_id_sync_profiles"
OLD_BINDING_TARGET_FK = (
    "fk_remote_bindings_target_connection_id_provider_connections"
)


def upgrade() -> None:
    bind = op.get_bind()
    _assert_tenant_references_are_clean(bind)
    if bind.dialect.name == "sqlite":
        _upgrade_sqlite()
    else:
        _upgrade_standard()


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _downgrade_sqlite()
    else:
        _downgrade_standard()


def _upgrade_standard() -> None:
    op.create_unique_constraint(
        PROVIDER_WORKSPACE_UNIQUE,
        "provider_connections",
        ["workspace_id", "id"],
    )
    op.create_unique_constraint(
        PROFILE_WORKSPACE_UNIQUE,
        "sync_profiles",
        ["workspace_id", "id"],
    )

    op.create_foreign_key(
        PROFILE_SOURCE_FK,
        "sync_profiles",
        "provider_connections",
        ["workspace_id", "source_connection_id"],
        ["workspace_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        PROFILE_TARGET_FK,
        "sync_profiles",
        "provider_connections",
        ["workspace_id", "target_connection_id"],
        ["workspace_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        RUN_PROFILE_FK,
        "sync_runs",
        "sync_profiles",
        ["workspace_id", "profile_id"],
        ["workspace_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        BINDING_PROFILE_FK,
        "remote_bindings",
        "sync_profiles",
        ["workspace_id", "profile_id"],
        ["workspace_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        BINDING_TARGET_FK,
        "remote_bindings",
        "provider_connections",
        ["workspace_id", "target_connection_id"],
        ["workspace_id", "id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        OLD_PROFILE_SOURCE_FK, "sync_profiles", type_="foreignkey"
    )
    op.drop_constraint(
        OLD_PROFILE_TARGET_FK, "sync_profiles", type_="foreignkey"
    )
    op.drop_constraint(OLD_RUN_PROFILE_FK, "sync_runs", type_="foreignkey")
    op.drop_constraint(
        OLD_BINDING_PROFILE_FK, "remote_bindings", type_="foreignkey"
    )
    op.drop_constraint(
        OLD_BINDING_TARGET_FK, "remote_bindings", type_="foreignkey"
    )


def _upgrade_sqlite() -> None:
    # SQLite cannot alter table constraints in place. Recreating only these four
    # tables keeps the compatibility path explicit while preserving all data and
    # indexes through Alembic's batch implementation.
    with op.batch_alter_table("provider_connections", recreate="always") as batch:
        batch.create_unique_constraint(
            PROVIDER_WORKSPACE_UNIQUE, ["workspace_id", "id"]
        )

    with op.batch_alter_table("sync_profiles", recreate="always") as batch:
        batch.create_unique_constraint(
            PROFILE_WORKSPACE_UNIQUE, ["workspace_id", "id"]
        )
        batch.create_foreign_key(
            PROFILE_SOURCE_FK,
            "provider_connections",
            ["workspace_id", "source_connection_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            PROFILE_TARGET_FK,
            "provider_connections",
            ["workspace_id", "target_connection_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint(OLD_PROFILE_SOURCE_FK, type_="foreignkey")
        batch.drop_constraint(OLD_PROFILE_TARGET_FK, type_="foreignkey")

    with op.batch_alter_table("sync_runs", recreate="always") as batch:
        batch.create_foreign_key(
            RUN_PROFILE_FK,
            "sync_profiles",
            ["workspace_id", "profile_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )
        batch.drop_constraint(OLD_RUN_PROFILE_FK, type_="foreignkey")

    with op.batch_alter_table("remote_bindings", recreate="always") as batch:
        batch.create_foreign_key(
            BINDING_PROFILE_FK,
            "sync_profiles",
            ["workspace_id", "profile_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            BINDING_TARGET_FK,
            "provider_connections",
            ["workspace_id", "target_connection_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )
        batch.drop_constraint(OLD_BINDING_PROFILE_FK, type_="foreignkey")
        batch.drop_constraint(OLD_BINDING_TARGET_FK, type_="foreignkey")


def _downgrade_standard() -> None:
    _create_legacy_foreign_keys(op)
    op.drop_constraint(BINDING_TARGET_FK, "remote_bindings", type_="foreignkey")
    op.drop_constraint(BINDING_PROFILE_FK, "remote_bindings", type_="foreignkey")
    op.drop_constraint(RUN_PROFILE_FK, "sync_runs", type_="foreignkey")
    op.drop_constraint(PROFILE_TARGET_FK, "sync_profiles", type_="foreignkey")
    op.drop_constraint(PROFILE_SOURCE_FK, "sync_profiles", type_="foreignkey")
    op.drop_constraint(PROFILE_WORKSPACE_UNIQUE, "sync_profiles", type_="unique")
    op.drop_constraint(
        PROVIDER_WORKSPACE_UNIQUE, "provider_connections", type_="unique"
    )


def _downgrade_sqlite() -> None:
    with op.batch_alter_table("remote_bindings", recreate="always") as batch:
        batch.create_foreign_key(
            OLD_BINDING_PROFILE_FK,
            "sync_profiles",
            ["profile_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            OLD_BINDING_TARGET_FK,
            "provider_connections",
            ["target_connection_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.drop_constraint(BINDING_PROFILE_FK, type_="foreignkey")
        batch.drop_constraint(BINDING_TARGET_FK, type_="foreignkey")

    with op.batch_alter_table("sync_runs", recreate="always") as batch:
        batch.create_foreign_key(
            OLD_RUN_PROFILE_FK,
            "sync_profiles",
            ["profile_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.drop_constraint(RUN_PROFILE_FK, type_="foreignkey")

    with op.batch_alter_table("sync_profiles", recreate="always") as batch:
        batch.create_foreign_key(
            OLD_PROFILE_SOURCE_FK,
            "provider_connections",
            ["source_connection_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            OLD_PROFILE_TARGET_FK,
            "provider_connections",
            ["target_connection_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint(PROFILE_SOURCE_FK, type_="foreignkey")
        batch.drop_constraint(PROFILE_TARGET_FK, type_="foreignkey")
        batch.drop_constraint(PROFILE_WORKSPACE_UNIQUE, type_="unique")

    with op.batch_alter_table("provider_connections", recreate="always") as batch:
        batch.drop_constraint(PROVIDER_WORKSPACE_UNIQUE, type_="unique")


def _create_legacy_foreign_keys(operations: object) -> None:
    operations.create_foreign_key(
        OLD_PROFILE_SOURCE_FK,
        "sync_profiles",
        "provider_connections",
        ["source_connection_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    operations.create_foreign_key(
        OLD_PROFILE_TARGET_FK,
        "sync_profiles",
        "provider_connections",
        ["target_connection_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    operations.create_foreign_key(
        OLD_RUN_PROFILE_FK,
        "sync_runs",
        "sync_profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    operations.create_foreign_key(
        OLD_BINDING_PROFILE_FK,
        "remote_bindings",
        "sync_profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    operations.create_foreign_key(
        OLD_BINDING_TARGET_FK,
        "remote_bindings",
        "provider_connections",
        ["target_connection_id"],
        ["id"],
        ondelete="CASCADE",
    )


def _assert_tenant_references_are_clean(bind: sa.engine.Connection) -> None:
    checks = (
        (
            "sync_profiles.source_connection_id",
            """
            SELECT COUNT(*)
            FROM sync_profiles child
            LEFT JOIN provider_connections parent
              ON parent.id = child.source_connection_id
            WHERE parent.id IS NULL OR parent.workspace_id <> child.workspace_id
            """,
        ),
        (
            "sync_profiles.target_connection_id",
            """
            SELECT COUNT(*)
            FROM sync_profiles child
            LEFT JOIN provider_connections parent
              ON parent.id = child.target_connection_id
            WHERE parent.id IS NULL OR parent.workspace_id <> child.workspace_id
            """,
        ),
        (
            "sync_runs.profile_id",
            """
            SELECT COUNT(*)
            FROM sync_runs child
            LEFT JOIN sync_profiles parent ON parent.id = child.profile_id
            WHERE parent.id IS NULL OR parent.workspace_id <> child.workspace_id
            """,
        ),
        (
            "remote_bindings.profile_id",
            """
            SELECT COUNT(*)
            FROM remote_bindings child
            LEFT JOIN sync_profiles parent ON parent.id = child.profile_id
            WHERE parent.id IS NULL OR parent.workspace_id <> child.workspace_id
            """,
        ),
        (
            "remote_bindings.target_connection_id",
            """
            SELECT COUNT(*)
            FROM remote_bindings child
            LEFT JOIN provider_connections parent
              ON parent.id = child.target_connection_id
            WHERE parent.id IS NULL OR parent.workspace_id <> child.workspace_id
            """,
        ),
    )
    violations: list[str] = []
    for label, query in checks:
        count = int(bind.scalar(sa.text(query)) or 0)
        if count > 0:
            violations.append(f"{label}={count}")
    if violations:
        raise RuntimeError(
            "tenant foreign-key migration refused inconsistent rows: "
            + ", ".join(violations)
        )
