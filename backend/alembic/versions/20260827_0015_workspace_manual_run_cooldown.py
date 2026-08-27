"""Add a configurable manual-run cooldown to workspaces.

Revision ID: 20260827_0015
Revises: 20260823_0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260827_0015"
down_revision = "20260823_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.add_column(
            sa.Column(
                "manual_run_cooldown_seconds",
                sa.Integer(),
                nullable=False,
                server_default="1800",
            )
        )
        batch_op.create_check_constraint(
            "manual_run_cooldown_allowed",
            "manual_run_cooldown_seconds IN (0, 300, 900, 1800)",
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.drop_constraint(
            "manual_run_cooldown_allowed",
            type_="check",
        )
        batch_op.drop_column("manual_run_cooldown_seconds")
