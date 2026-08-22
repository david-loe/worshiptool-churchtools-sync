"""Index pending notification fanout scans.

Revision ID: 20260822_0007
Revises: 20260822_0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260822_0007"
down_revision = "20260822_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_sync_runs_notification_fanout",
        "sync_runs",
        ["status"],
        unique=False,
        postgresql_where=sa.text("notifications_fanned_out_at IS NULL"),
        sqlite_where=sa.text("notifications_fanned_out_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sync_runs_notification_fanout",
        table_name="sync_runs",
    )
