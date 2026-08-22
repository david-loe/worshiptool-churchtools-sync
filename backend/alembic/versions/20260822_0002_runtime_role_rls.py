"""Runtime grants and PostgreSQL tenant policies.

Revision ID: 20260822_0002
Revises: 20260822_0001
"""

from __future__ import annotations

from alembic import op


revision = "20260822_0002"
down_revision = "20260822_0001"
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "workspace_invitations",
    "provider_connections",
    "sync_profiles",
    "sync_runs",
    "remote_bindings",
    "notifications",
    "notification_preferences",
    "push_subscriptions",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Runtime containers deliberately use a non-owner database role. The role
    # is created by the Compose init hook; the conditional block also keeps
    # external PostgreSQL installations easy to migrate before role setup.
    op.execute(
        """
        DO $grant_runtime$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_app') THEN
            GRANT USAGE ON SCHEMA public TO worshipsync_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
              TO worshipsync_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO worshipsync_app;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
              GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO worshipsync_app;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
              GRANT USAGE, SELECT ON SEQUENCES TO worshipsync_app;
          END IF;
        END
        $grant_runtime$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_workspace_access(target_workspace uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $policy$
          SELECT
            current_setting('app.worker', true) = '1'
            OR current_setting('app.platform_admin', true) = '1'
            OR EXISTS (
              SELECT 1
              FROM memberships membership
              WHERE membership.workspace_id = target_workspace
                AND membership.user_id = NULLIF(
                  current_setting('app.user_id', true), ''
                )::uuid
            )
        $policy$;

        REVOKE ALL ON FUNCTION app_workspace_access(uuid) FROM PUBLIC;
        """
    )
    op.execute(
        """
        DO $grant_policy$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_app') THEN
            GRANT EXECUTE ON FUNCTION app_workspace_access(uuid) TO worshipsync_app;
          END IF;
        END
        $grant_policy$;
        """
    )

    for table in TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY tenant_workspace_policy ON "{table}" '
            f'USING (app_workspace_access("{table}".workspace_id)) '
            f'WITH CHECK (app_workspace_access("{table}".workspace_id))'
        )

    # Actions inherit their tenant from the parent run and therefore need a
    # separate policy expression.
    op.execute("ALTER TABLE sync_actions ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_run_action_policy ON sync_actions
        USING (
          EXISTS (
            SELECT 1 FROM sync_runs run
            WHERE run.id = sync_actions.run_id
              AND app_workspace_access(run.workspace_id)
          )
        )
        WITH CHECK (
          EXISTS (
            SELECT 1 FROM sync_runs run
            WHERE run.id = sync_actions.run_id
              AND app_workspace_access(run.workspace_id)
          )
        )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP POLICY IF EXISTS tenant_run_action_policy ON sync_actions")
    op.execute("ALTER TABLE sync_actions DISABLE ROW LEVEL SECURITY")
    for table in reversed(TENANT_TABLES):
        op.execute(f'DROP POLICY IF EXISTS tenant_workspace_policy ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute("DROP FUNCTION IF EXISTS app_workspace_access(uuid)")
