"""Fence notification outbox delivery claims.

Revision ID: 20260822_0006
Revises: 20260822_0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260822_0006"
down_revision = "20260822_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_outbox",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_outbox", "claim_token")
