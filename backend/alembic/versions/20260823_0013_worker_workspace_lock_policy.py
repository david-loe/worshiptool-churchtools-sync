"""Allow worker workspace locks while rejecting workspace mutations.

Revision ID: 20260823_0013
Revises: 20260823_0012
"""

from __future__ import annotations

from alembic import op


revision = "20260823_0013"
down_revision = "20260823_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # SELECT ... FOR UPDATE is checked against UPDATE row policies. Expose the
    # existing row so workers can serialize run claims against archival, while
    # rejecting every changed row through the WITH CHECK expression.
    op.execute(
        "DROP POLICY IF EXISTS worker_workspace_lock_policy ON workspaces"
    )
    op.execute(
        """
        CREATE POLICY worker_workspace_lock_policy ON workspaces
        FOR UPDATE TO worshipsync_worker
        USING (true)
        WITH CHECK (false)
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "DROP POLICY IF EXISTS worker_workspace_lock_policy ON workspaces"
    )
