"""Throttle durable broker redelivery attempts.

Revision ID: 20260822_0004
Revises: 20260822_0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260822_0004"
down_revision = "20260822_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sync_runs")}
    if "dispatch_attempted_at" not in columns:
        op.add_column(
            "sync_runs",
            sa.Column("dispatch_attempted_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("sync_runs")}
    if "ix_sync_runs_dispatch_recovery" not in indexes:
        op.create_index(
            "ix_sync_runs_dispatch_recovery",
            "sync_runs",
            ["status", "dispatch_attempted_at", "lease_expires_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("sync_runs")}
    if "ix_sync_runs_dispatch_recovery" in indexes:
        op.drop_index("ix_sync_runs_dispatch_recovery", table_name="sync_runs")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sync_runs")}
    if "dispatch_attempted_at" in columns:
        op.drop_column("sync_runs", "dispatch_attempted_at")
