"""Add per-profile sync modes and verified WT event snapshots.

Revision ID: 20260823_0014
Revises: 20260823_0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260823_0014"
down_revision = "20260823_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_profiles",
        sa.Column(
            "sync_mode",
            sa.String(length=32),
            nullable=False,
            server_default="source_changes_only",
        ),
    )
    op.create_table(
        "event_sync_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.String(length=200), nullable=False),
        sa.Column("target_event_id", sa.String(length=200), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "profile_id"],
            ["sync_profiles.workspace_id", "sync_profiles.id"],
            name="fk_event_sync_states_workspace_profile",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "source_event_id",
            "target_event_id",
            name="uq_event_sync_states_profile_event_pair",
        ),
    )
    op.create_index(
        "ix_event_sync_states_workspace_profile",
        "event_sync_states",
        ["workspace_id", "profile_id"],
        unique=False,
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE event_sync_states TO worshipsync_worker"
        )
        op.execute("ALTER TABLE event_sync_states ENABLE ROW LEVEL SECURITY")
        op.execute(
            """
            CREATE POLICY worker_event_sync_states_policy ON event_sync_states
            FOR ALL TO worshipsync_worker
            USING (true)
            WITH CHECK (true)
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP POLICY IF EXISTS worker_event_sync_states_policy ON event_sync_states"
        )
    op.drop_index(
        "ix_event_sync_states_workspace_profile", table_name="event_sync_states"
    )
    op.drop_table("event_sync_states")
    op.drop_column("sync_profiles", "sync_mode")
