"""Persist the next profile execution for indexed, starvation-free scheduling.

Revision ID: 20260822_0003
Revises: 20260822_0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260822_0003"
down_revision = "20260822_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("sync_profiles")}
    if "next_scheduled_at" not in columns:
        op.add_column(
            "sync_profiles",
            sa.Column("next_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        )
    profiles = sa.table(
        "sync_profiles",
        sa.column("enabled", sa.Boolean()),
        sa.column("next_scheduled_at", sa.DateTime(timezone=True)),
    )
    # Existing enabled profiles become immediately eligible once. Subsequent
    # times are calculated by the scheduler from their configured timezone.
    op.execute(
        profiles.update()
        .where(
            profiles.c.enabled.is_(True),
            profiles.c.next_scheduled_at.is_(None),
        )
        .values(next_scheduled_at=sa.func.now())
    )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("sync_profiles")}
    if "ix_sync_profiles_due" not in indexes:
        op.create_index(
            "ix_sync_profiles_due",
            "sync_profiles",
            ["enabled", "next_scheduled_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("sync_profiles")}
    if "ix_sync_profiles_due" in indexes:
        op.drop_index("ix_sync_profiles_due", table_name="sync_profiles")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sync_profiles")}
    if "next_scheduled_at" in columns:
        op.drop_column("sync_profiles", "next_scheduled_at")
