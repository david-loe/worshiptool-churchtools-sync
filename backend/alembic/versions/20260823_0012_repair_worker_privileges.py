"""Permit background workers to lock workspace rows safely.

Revision ID: 20260823_0012
Revises: 20260822_0011
"""

from __future__ import annotations

from alembic import op


revision = "20260823_0012"
down_revision = "20260822_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # PostgreSQL requires an UPDATE privilege for SELECT ... FOR UPDATE even
    # when the statement never mutates a row. Worker claim and scheduler paths
    # lock workspaces to serialize against archival, but revision 0008 granted
    # only SELECT. A column-scoped grant supplies the lock capability without
    # granting broad workspace mutation; RLS still has no worker UPDATE policy.
    op.execute(
        """
        DO $worker_role$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_worker'
          ) THEN
            RAISE EXCEPTION
              'Missing database role: worshipsync_worker. Run deploy/postgres/001-create-app-role.sh first.';
          END IF;
        END
        $worker_role$;
        """
    )
    op.execute(
        "GRANT UPDATE (updated_at) ON TABLE workspaces TO worshipsync_worker"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "REVOKE UPDATE (updated_at) ON TABLE workspaces FROM worshipsync_worker"
    )
