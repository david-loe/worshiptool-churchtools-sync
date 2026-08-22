"""Make quota checks RLS-complete and narrow API update columns.

Revision ID: 20260822_0011
Revises: 20260822_0010
"""

from __future__ import annotations

from alembic import op


revision = "20260822_0011"
down_revision = "20260822_0010"
branch_labels = None
depends_on = None


API_ROLE = "worshipsync_api"
WORKER_ROLE = "worshipsync_worker"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # Ownership quota checks must include workspaces hidden from the acting
    # tenant by membership RLS.  Return only the aggregate needed by the API.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_owned_workspace_count(target_user uuid)
        RETURNS integer
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $quota$
          SELECT count(*)::integer
          FROM public.memberships AS membership
          WHERE membership.user_id = target_user
            AND membership.role = 'owner'
        $quota$;

        REVOKE ALL ON FUNCTION app_owned_workspace_count(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app_owned_workspace_count(uuid)
          TO worshipsync_api;
        """
    )

    # Normal tenant routes can rename/archive a workspace. Quota changes stay
    # on the separately authenticated platform-admin engine/role.
    op.execute(f"REVOKE UPDATE ON TABLE workspaces FROM {API_ROLE}")
    op.execute(
        f"GRANT UPDATE (name, archived_at, updated_at) "
        f"ON TABLE workspaces TO {API_ROLE}"
    )

    # Authentication flows update only these account-owned security fields.
    # In particular, the shared API role cannot activate accounts or promote
    # them to platform administrator through an accidental broad UPDATE.
    op.execute(f"REVOKE UPDATE ON TABLE users FROM {API_ROLE}")
    op.execute(
        f"GRANT UPDATE (password_hash, email_verified_at, "
        f"totp_secret_encrypted, totp_pending_secret_encrypted, "
        f"totp_recovery_hashes, updated_at) ON TABLE users TO {API_ROLE}"
    )
    # The background role only rewrites encrypted TOTP material during master
    # key rotation; it must not be able to alter passwords or account flags.
    op.execute(f"REVOKE UPDATE ON TABLE users FROM {WORKER_ROLE}")
    op.execute(
        f"GRANT UPDATE (totp_secret_encrypted, "
        f"totp_pending_secret_encrypted, updated_at) "
        f"ON TABLE users TO {WORKER_ROLE}"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(f"GRANT UPDATE ON TABLE users TO {API_ROLE}, {WORKER_ROLE}")
    op.execute(f"GRANT UPDATE ON TABLE workspaces TO {API_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS app_owned_workspace_count(uuid)")
