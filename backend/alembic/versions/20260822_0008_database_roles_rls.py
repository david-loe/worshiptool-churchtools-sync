"""Separate API, worker, and platform-admin database capabilities.

Revision ID: 20260822_0008
Revises: 20260822_0007
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op


revision = "20260822_0008"
down_revision = "20260822_0007"
branch_labels = None
depends_on = None


API_ROLE = "worshipsync_api"
WORKER_ROLE = "worshipsync_worker"
ADMIN_ROLE = "worshipsync_admin"
RUNTIME_ROLES = (API_ROLE, WORKER_ROLE, ADMIN_ROLE)

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


def _grant(privileges: str, tables: Iterable[str], role: str) -> None:
    table_list = ", ".join(f'"{table}"' for table in tables)
    op.execute(f"GRANT {privileges} ON TABLE {table_list} TO {role}")


def _require_runtime_roles() -> None:
    op.execute(
        """
        DO $roles$
        DECLARE
          missing_roles text;
          unsafe_roles text;
          inherited_roles text;
        BEGIN
          SELECT string_agg(required_role, ', ' ORDER BY required_role)
            INTO missing_roles
          FROM unnest(ARRAY[
            'worshipsync_api',
            'worshipsync_worker',
            'worshipsync_admin'
          ]) AS required_roles(required_role)
          WHERE NOT EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = required_role
          );

          IF missing_roles IS NOT NULL THEN
            RAISE EXCEPTION
              'Missing database roles: %. Run deploy/postgres/001-create-app-role.sh before Alembic.',
              missing_roles;
          END IF;

          SELECT string_agg(rolname, ', ' ORDER BY rolname)
            INTO unsafe_roles
          FROM pg_roles
          WHERE rolname IN (
            'worshipsync_api', 'worshipsync_worker', 'worshipsync_admin'
          )
            AND (
              NOT rolcanlogin
              OR rolsuper
              OR rolinherit
              OR rolcreaterole
              OR rolcreatedb
              OR rolreplication
              OR rolbypassrls
            );

          IF unsafe_roles IS NOT NULL THEN
            RAISE EXCEPTION
              'Unsafe database role attributes on: %. Re-run deploy/postgres/001-create-app-role.sh.',
              unsafe_roles;
          END IF;

          SELECT string_agg(
                   member_role.rolname || ' -> ' || granted_role.rolname,
                   ', ' ORDER BY member_role.rolname, granted_role.rolname
                 )
            INTO inherited_roles
          FROM pg_auth_members membership
          JOIN pg_roles member_role ON member_role.oid = membership.member
          JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
          WHERE member_role.rolname IN (
            'worshipsync_api', 'worshipsync_worker', 'worshipsync_admin'
          );

          IF inherited_roles IS NOT NULL THEN
            RAISE EXCEPTION
              'Runtime database roles must not be members of other roles: %.',
              inherited_roles;
          END IF;
        END
        $roles$;
        """
    )


def _remove_legacy_role_access() -> None:
    # Revision 0002 granted the former shared role every table and configured
    # equally broad default privileges. Remove both so old credentials cannot
    # retain a quiet cross-tenant back door after the cutover.
    op.execute(
        """
        DO $legacy$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_app') THEN
            EXECUTE
              'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM worshipsync_app';
            EXECUTE
              'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM worshipsync_app';
            EXECUTE
              'REVOKE ALL ON FUNCTION app_workspace_access(uuid) FROM worshipsync_app';
            EXECUTE
              'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
              'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM worshipsync_app';
            EXECUTE
              'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
              'REVOKE USAGE, SELECT ON SEQUENCES FROM worshipsync_app';
            EXECUTE format(
              'REVOKE CONNECT ON DATABASE %I FROM worshipsync_app',
              current_database()
            );
          END IF;
        END
        $legacy$;
        """
    )


def _reset_runtime_privileges() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(
        f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {roles}"
    )
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {roles}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {roles}")
    op.execute(
        """
        DO $connect$
        BEGIN
          EXECUTE format(
            'GRANT CONNECT ON DATABASE %I TO worshipsync_api, worshipsync_worker, worshipsync_admin',
            current_database()
          );
        END
        $connect$;
        """
    )


def _grant_runtime_privileges() -> None:
    _grant("SELECT, INSERT, UPDATE", ("users",), API_ROLE)
    _grant("SELECT, INSERT, UPDATE, DELETE", ("auth_sessions",), API_ROLE)
    _grant("SELECT, INSERT, UPDATE, DELETE", ("one_time_tokens",), API_ROLE)
    _grant("SELECT, INSERT, UPDATE", ("workspaces",), API_ROLE)
    _grant("SELECT, INSERT, UPDATE, DELETE", ("memberships",), API_ROLE)
    _grant(
        "SELECT, INSERT, UPDATE, DELETE",
        ("workspace_invitations", "provider_connections", "sync_profiles"),
        API_ROLE,
    )
    _grant("SELECT, INSERT, UPDATE", ("sync_runs",), API_ROLE)
    _grant("SELECT", ("sync_actions", "remote_bindings"), API_ROLE)
    _grant("SELECT, UPDATE", ("notifications",), API_ROLE)
    _grant(
        "SELECT, INSERT, UPDATE", ("notification_preferences",), API_ROLE
    )
    _grant(
        "SELECT, INSERT, UPDATE, DELETE", ("push_subscriptions",), API_ROLE
    )
    _grant("SELECT, INSERT", ("notification_outbox",), API_ROLE)

    # Background services share one role because scheduler, sync, fanout,
    # retention, and key rotation are all trusted cross-tenant jobs. The role
    # is never injected into the API container.
    _grant("SELECT, UPDATE", ("users",), WORKER_ROLE)
    _grant("SELECT, DELETE", ("auth_sessions", "one_time_tokens"), WORKER_ROLE)
    _grant("SELECT", ("workspaces", "memberships"), WORKER_ROLE)
    _grant("SELECT, DELETE", ("workspace_invitations",), WORKER_ROLE)
    _grant("SELECT, UPDATE", ("provider_connections", "sync_profiles"), WORKER_ROLE)
    _grant("SELECT, INSERT, UPDATE, DELETE", ("sync_runs",), WORKER_ROLE)
    _grant(
        "SELECT, INSERT, UPDATE, DELETE",
        ("sync_actions", "remote_bindings", "notifications"),
        WORKER_ROLE,
    )
    _grant("SELECT", ("notification_preferences",), WORKER_ROLE)
    _grant("SELECT, UPDATE, DELETE", ("push_subscriptions",), WORKER_ROLE)
    _grant(
        "SELECT, INSERT, UPDATE, DELETE", ("notification_outbox",), WORKER_ROLE
    )
    _grant("SELECT, DELETE", ("audit_events",), WORKER_ROLE)

    # The second API engine is reachable only after the regular session has
    # passed platform-admin + MFA checks. Its SQL role remains deliberately
    # narrower than the worker role and has no BYPASSRLS attribute.
    _grant("SELECT, UPDATE", ("workspaces",), ADMIN_ROLE)
    _grant("SELECT", ("memberships", "sync_profiles"), ADMIN_ROLE)
    _grant("INSERT", ("audit_events",), ADMIN_ROLE)


def _install_access_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_workspace_access(target_workspace uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY INVOKER
        SET search_path = public, pg_temp
        AS $policy$
          SELECT
            current_user = 'worshipsync_worker'
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
        REVOKE ALL ON FUNCTION app_workspace_access(uuid)
          FROM worshipsync_admin;
        GRANT EXECUTE ON FUNCTION app_workspace_access(uuid)
          TO worshipsync_api, worshipsync_worker;
        """
    )


def _install_tenant_policies() -> None:
    for table in TENANT_TABLES:
        op.execute(f'DROP POLICY IF EXISTS tenant_workspace_policy ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY tenant_workspace_policy ON "{table}" '
            f"TO {API_ROLE}, {WORKER_ROLE} "
            f'USING (app_workspace_access("{table}".workspace_id)) '
            f'WITH CHECK (app_workspace_access("{table}".workspace_id))'
        )

    op.execute("DROP POLICY IF EXISTS tenant_run_action_policy ON sync_actions")
    op.execute("ALTER TABLE sync_actions ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_run_action_policy ON sync_actions
        TO {API_ROLE}, {WORKER_ROLE}
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

    # Revision 0005 policies were PUBLIC. Keep their narrow semantics, but
    # expose the invitation-recipient path only to the API role.
    op.execute(
        "DROP POLICY IF EXISTS invitation_recipient_select_policy "
        "ON workspace_invitations"
    )
    op.execute(
        "DROP POLICY IF EXISTS invitation_recipient_lock_policy "
        "ON workspace_invitations"
    )
    op.execute(
        f"""
        CREATE POLICY invitation_recipient_select_policy
        ON workspace_invitations
        FOR SELECT TO {API_ROLE}
        USING (
          workspace_invitations.accepted_at IS NULL
          AND workspace_invitations.expires_at > CURRENT_TIMESTAMP
          AND EXISTS (
            SELECT 1
            FROM users invitation_user
            WHERE invitation_user.id = NULLIF(
              current_setting('app.user_id', true), ''
            )::uuid
              AND invitation_user.is_active IS TRUE
              AND invitation_user.normalized_email =
                workspace_invitations.normalized_email
          )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY invitation_recipient_lock_policy
        ON workspace_invitations
        FOR UPDATE TO {API_ROLE}
        USING (
          workspace_invitations.accepted_at IS NULL
          AND workspace_invitations.expires_at > CURRENT_TIMESTAMP
          AND EXISTS (
            SELECT 1
            FROM users invitation_user
            WHERE invitation_user.id = NULLIF(
              current_setting('app.user_id', true), ''
            )::uuid
              AND invitation_user.is_active IS TRUE
              AND invitation_user.normalized_email =
                workspace_invitations.normalized_email
          )
        )
        WITH CHECK (app_workspace_access(workspace_invitations.workspace_id))
        """
    )


def _install_outbox_and_audit_policies() -> None:
    op.execute("ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY")
    op.execute(
        "DROP POLICY IF EXISTS api_outbox_select_policy ON notification_outbox"
    )
    op.execute(
        "DROP POLICY IF EXISTS api_outbox_insert_policy ON notification_outbox"
    )
    op.execute(
        "DROP POLICY IF EXISTS worker_outbox_policy ON notification_outbox"
    )
    op.execute(
        f"""
        CREATE POLICY api_outbox_select_policy ON notification_outbox
        FOR SELECT TO {API_ROLE}
        USING (
          notification_outbox.workspace_id IS NOT NULL
          AND app_workspace_access(notification_outbox.workspace_id)
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY api_outbox_insert_policy ON notification_outbox
        FOR INSERT TO {API_ROLE}
        WITH CHECK (
          notification_outbox.workspace_id IS NULL
          OR app_workspace_access(notification_outbox.workspace_id)
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY worker_outbox_policy ON notification_outbox
        FOR ALL TO {WORKER_ROLE}
        USING (true)
        WITH CHECK (true)
        """
    )

    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS worker_audit_policy ON audit_events")
    op.execute("DROP POLICY IF EXISTS admin_audit_insert_policy ON audit_events")
    op.execute(
        f"""
        CREATE POLICY worker_audit_policy ON audit_events
        FOR ALL TO {WORKER_ROLE}
        USING (true)
        WITH CHECK (true)
        """
    )
    op.execute(
        f"""
        CREATE POLICY admin_audit_insert_policy ON audit_events
        FOR INSERT TO {ADMIN_ROLE}
        WITH CHECK (audit_events.workspace_id IS NOT NULL)
        """
    )

    # Platform administrators need cross-tenant counts, not access to provider
    # credentials or the rest of the tenant graph.
    op.execute("DROP POLICY IF EXISTS admin_profiles_select_policy ON sync_profiles")
    op.execute(
        f"""
        CREATE POLICY admin_profiles_select_policy ON sync_profiles
        FOR SELECT TO {ADMIN_ROLE}
        USING (true)
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _require_runtime_roles()
    _remove_legacy_role_access()
    _reset_runtime_privileges()
    _grant_runtime_privileges()
    _install_access_function()
    _install_tenant_policies()
    _install_outbox_and_audit_policies()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP POLICY IF EXISTS admin_profiles_select_policy ON sync_profiles")
    op.execute("DROP POLICY IF EXISTS admin_audit_insert_policy ON audit_events")
    op.execute("DROP POLICY IF EXISTS worker_audit_policy ON audit_events")
    op.execute("ALTER TABLE audit_events DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS worker_outbox_policy ON notification_outbox")
    op.execute("DROP POLICY IF EXISTS api_outbox_insert_policy ON notification_outbox")
    op.execute("DROP POLICY IF EXISTS api_outbox_select_policy ON notification_outbox")
    op.execute("ALTER TABLE notification_outbox DISABLE ROW LEVEL SECURITY")

    op.execute(
        "DROP POLICY IF EXISTS invitation_recipient_lock_policy "
        "ON workspace_invitations"
    )
    op.execute(
        "DROP POLICY IF EXISTS invitation_recipient_select_policy "
        "ON workspace_invitations"
    )
    op.execute("DROP POLICY IF EXISTS tenant_run_action_policy ON sync_actions")
    for table in reversed(TENANT_TABLES):
        op.execute(f'DROP POLICY IF EXISTS tenant_workspace_policy ON "{table}"')

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
        REVOKE ALL ON FUNCTION app_workspace_access(uuid)
          FROM worshipsync_api, worshipsync_worker, worshipsync_admin;
        """
    )

    for table in TENANT_TABLES:
        op.execute(
            f'CREATE POLICY tenant_workspace_policy ON "{table}" '
            f'USING (app_workspace_access("{table}".workspace_id)) '
            f'WITH CHECK (app_workspace_access("{table}".workspace_id))'
        )
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
    op.execute(
        """
        CREATE POLICY invitation_recipient_select_policy
        ON workspace_invitations
        FOR SELECT
        USING (
          workspace_invitations.accepted_at IS NULL
          AND workspace_invitations.expires_at > CURRENT_TIMESTAMP
          AND EXISTS (
            SELECT 1 FROM users invitation_user
            WHERE invitation_user.id = NULLIF(
              current_setting('app.user_id', true), ''
            )::uuid
              AND invitation_user.is_active IS TRUE
              AND invitation_user.normalized_email =
                workspace_invitations.normalized_email
          )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY invitation_recipient_lock_policy
        ON workspace_invitations
        FOR UPDATE
        USING (
          workspace_invitations.accepted_at IS NULL
          AND workspace_invitations.expires_at > CURRENT_TIMESTAMP
          AND EXISTS (
            SELECT 1 FROM users invitation_user
            WHERE invitation_user.id = NULLIF(
              current_setting('app.user_id', true), ''
            )::uuid
              AND invitation_user.is_active IS TRUE
              AND invitation_user.normalized_email =
                workspace_invitations.normalized_email
          )
        )
        WITH CHECK (app_workspace_access(workspace_invitations.workspace_id))
        """
    )

    roles = ", ".join(RUNTIME_ROLES)
    op.execute(
        f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {roles}")
    op.execute(
        """
        DO $legacy$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_app') THEN
            EXECUTE 'GRANT USAGE ON SCHEMA public TO worshipsync_app';
            EXECUTE
              'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public '
              'TO worshipsync_app';
            EXECUTE
              'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO worshipsync_app';
            EXECUTE
              'GRANT EXECUTE ON FUNCTION app_workspace_access(uuid) TO worshipsync_app';
            EXECUTE format(
              'GRANT CONNECT ON DATABASE %I TO worshipsync_app', current_database()
            );
          END IF;
        END
        $legacy$;
        """
    )
