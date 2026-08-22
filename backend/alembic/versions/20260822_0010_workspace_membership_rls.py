"""Fence workspace and membership rows at the database boundary.

Revision ID: 20260822_0010
Revises: 20260822_0009

Memberships are also the source of tenant authorization.  Small,
owner-executed predicate functions avoid recursive membership RLS while keeping
the freely settable request GUC useful only as an identity that must match a
real membership or invitation.
"""

from __future__ import annotations

from alembic import op


revision = "20260822_0010"
down_revision = "20260822_0009"
branch_labels = None
depends_on = None


API_ROLE = "worshipsync_api"
WORKER_ROLE = "worshipsync_worker"
ADMIN_ROLE = "worshipsync_admin"


def _install_predicates() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_user_workspace_role(target_workspace uuid)
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $policy$
          SELECT membership.role
          FROM public.memberships AS membership
          WHERE membership.workspace_id = target_workspace
            AND membership.user_id = NULLIF(
              current_setting('app.user_id', true), ''
            )::uuid
          LIMIT 1
        $policy$;

        REVOKE ALL ON FUNCTION app_user_workspace_role(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app_user_workspace_role(uuid)
          TO worshipsync_api, worshipsync_worker;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_user_has_valid_invitation(
          target_workspace uuid
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $policy$
          SELECT EXISTS (
            SELECT 1
            FROM public.workspace_invitations AS invitation
            JOIN public.users AS invitation_user
              ON invitation_user.normalized_email = invitation.normalized_email
            WHERE invitation.workspace_id = target_workspace
              AND invitation.accepted_at IS NULL
              AND invitation.expires_at > CURRENT_TIMESTAMP
              AND invitation_user.id = NULLIF(
                current_setting('app.user_id', true), ''
              )::uuid
              AND invitation_user.is_active IS TRUE
          )
        $policy$;

        REVOKE ALL ON FUNCTION app_user_has_valid_invitation(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app_user_has_valid_invitation(uuid)
          TO worshipsync_api;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_workspace_is_unclaimed(
          target_workspace uuid
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $policy$
          SELECT NOT EXISTS (
            SELECT 1
            FROM public.memberships AS membership
            WHERE membership.workspace_id = target_workspace
          )
        $policy$;

        REVOKE ALL ON FUNCTION app_workspace_is_unclaimed(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app_workspace_is_unclaimed(uuid)
          TO worshipsync_api;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_membership_insert_allowed(
          target_workspace uuid,
          target_user uuid,
          target_role text
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $policy$
          SELECT
            target_user = NULLIF(
              current_setting('app.user_id', true), ''
            )::uuid
            AND (
              (
                target_role = 'owner'
                AND NOT EXISTS (
                  SELECT 1
                  FROM public.memberships AS existing_membership
                  WHERE existing_membership.workspace_id = target_workspace
                )
              )
              OR EXISTS (
                SELECT 1
                FROM public.workspace_invitations AS invitation
                JOIN public.users AS invitation_user
                  ON invitation_user.normalized_email = invitation.normalized_email
                WHERE invitation.workspace_id = target_workspace
                  AND invitation.role = target_role
                  AND invitation.accepted_at IS NULL
                  AND invitation.expires_at > CURRENT_TIMESTAMP
                  AND invitation_user.id = target_user
                  AND invitation_user.is_active IS TRUE
              )
            )
        $policy$;

        REVOKE ALL ON FUNCTION app_membership_insert_allowed(uuid, uuid, text)
          FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app_membership_insert_allowed(uuid, uuid, text)
          TO worshipsync_api;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_membership_existing_manage_allowed(
          target_workspace uuid,
          target_role text
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $policy$
          SELECT CASE public.app_user_workspace_role(target_workspace)
            WHEN 'owner' THEN
              target_role <> 'owner'
              OR (
                SELECT count(*) > 1
                FROM public.memberships AS workspace_owner
                WHERE workspace_owner.workspace_id = target_workspace
                  AND workspace_owner.role = 'owner'
              )
            WHEN 'admin' THEN target_role <> 'owner'
            ELSE false
          END
        $policy$;

        REVOKE ALL ON FUNCTION app_membership_existing_manage_allowed(uuid, text)
          FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app_membership_existing_manage_allowed(uuid, text)
          TO worshipsync_api;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_membership_new_role_allowed(
          target_workspace uuid,
          target_role text
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $policy$
          SELECT CASE public.app_user_workspace_role(target_workspace)
            WHEN 'owner' THEN true
            WHEN 'admin' THEN target_role <> 'owner'
            ELSE false
          END
        $policy$;

        REVOKE ALL ON FUNCTION app_membership_new_role_allowed(uuid, text)
          FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app_membership_new_role_allowed(uuid, text)
          TO worshipsync_api;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_workspace_access(target_workspace uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY INVOKER
        SET search_path = pg_catalog, public
        AS $policy$
          SELECT
            current_user = 'worshipsync_worker'
            OR public.app_user_workspace_role(target_workspace) IS NOT NULL
        $policy$;

        REVOKE ALL ON FUNCTION app_workspace_access(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION app_workspace_access(uuid)
          FROM worshipsync_admin;
        GRANT EXECUTE ON FUNCTION app_workspace_access(uuid)
          TO worshipsync_api, worshipsync_worker;
        """
    )


def _install_workspace_policies() -> None:
    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    for policy in (
        "api_workspace_select_policy",
        "api_workspace_insert_policy",
        "api_workspace_update_policy",
        "worker_workspace_select_policy",
        "admin_workspace_policy",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON workspaces")

    op.execute(
        f"""
        CREATE POLICY api_workspace_select_policy ON workspaces
        FOR SELECT TO {API_ROLE}
        USING (
          app_workspace_access(workspaces.id)
          OR app_user_has_valid_invitation(workspaces.id)
          OR app_workspace_is_unclaimed(workspaces.id)
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY api_workspace_insert_policy ON workspaces
        FOR INSERT TO {API_ROLE}
        WITH CHECK (
          NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY api_workspace_update_policy ON workspaces
        FOR UPDATE TO {API_ROLE}
        USING (
          app_user_workspace_role(workspaces.id) IN ('owner', 'admin')
          OR app_user_has_valid_invitation(workspaces.id)
        )
        WITH CHECK (
          app_user_workspace_role(workspaces.id) IN ('owner', 'admin')
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY worker_workspace_select_policy ON workspaces
        FOR SELECT TO {WORKER_ROLE}
        USING (true)
        """
    )
    op.execute(
        f"""
        CREATE POLICY admin_workspace_policy ON workspaces
        FOR ALL TO {ADMIN_ROLE}
        USING (true)
        WITH CHECK (true)
        """
    )


def _install_membership_policies() -> None:
    op.execute("ALTER TABLE memberships ENABLE ROW LEVEL SECURITY")
    # A row policy can validate the old and new role, but it cannot compare
    # OLD.user_id/workspace_id with their new values.  Keep those identity
    # columns immutable to the API at the privilege layer so a tenant admin
    # cannot turn an existing row into an uninvited membership.
    op.execute(f"REVOKE UPDATE ON TABLE memberships FROM {API_ROLE}")
    op.execute(
        f"GRANT UPDATE (role, updated_at) ON TABLE memberships TO {API_ROLE}"
    )
    for policy in (
        "api_membership_select_policy",
        "api_membership_insert_policy",
        "api_membership_update_policy",
        "api_membership_delete_policy",
        "worker_membership_select_policy",
        "admin_membership_select_policy",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON memberships")

    op.execute(
        f"""
        CREATE POLICY api_membership_select_policy ON memberships
        FOR SELECT TO {API_ROLE}
        USING (app_workspace_access(memberships.workspace_id))
        """
    )
    op.execute(
        f"""
        CREATE POLICY api_membership_insert_policy ON memberships
        FOR INSERT TO {API_ROLE}
        WITH CHECK (
          app_membership_insert_allowed(
            memberships.workspace_id,
            memberships.user_id,
            memberships.role
          )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY api_membership_update_policy ON memberships
        FOR UPDATE TO {API_ROLE}
        USING (
          app_membership_existing_manage_allowed(
            memberships.workspace_id,
            memberships.role
          )
        )
        WITH CHECK (
          app_membership_new_role_allowed(
            memberships.workspace_id,
            memberships.role
          )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY api_membership_delete_policy ON memberships
        FOR DELETE TO {API_ROLE}
        USING (
          app_membership_existing_manage_allowed(
            memberships.workspace_id,
            memberships.role
          )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY worker_membership_select_policy ON memberships
        FOR SELECT TO {WORKER_ROLE}
        USING (true)
        """
    )
    op.execute(
        f"""
        CREATE POLICY admin_membership_select_policy ON memberships
        FOR SELECT TO {ADMIN_ROLE}
        USING (true)
        """
    )


def _install_account_outbox_policies() -> None:
    # Account e-mails may be system-wide (workspace_id NULL) when a user such
    # as a bootstrap platform administrator has no membership.  The API may
    # see and insert only its current account's own e-mail row, which keeps the
    # idempotency lookup/race recovery functional without exposing other
    # system-wide messages.
    op.execute(
        "DROP POLICY IF EXISTS api_outbox_select_policy ON notification_outbox"
    )
    op.execute(
        "DROP POLICY IF EXISTS api_outbox_insert_policy ON notification_outbox"
    )
    op.execute(
        f"""
        CREATE POLICY api_outbox_select_policy ON notification_outbox
        FOR SELECT TO {API_ROLE}
        USING (
          (
            notification_outbox.workspace_id IS NOT NULL
            AND app_workspace_access(notification_outbox.workspace_id)
          )
          OR (
            notification_outbox.workspace_id IS NULL
            AND notification_outbox.notification_id IS NULL
            AND notification_outbox.channel = 'email'
            AND EXISTS (
              SELECT 1
              FROM users AS account_user
              WHERE account_user.id = NULLIF(
                current_setting('app.user_id', true), ''
              )::uuid
                AND account_user.email = notification_outbox.recipient
            )
          )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY api_outbox_insert_policy ON notification_outbox
        FOR INSERT TO {API_ROLE}
        WITH CHECK (
          (
            notification_outbox.workspace_id IS NOT NULL
            AND app_workspace_access(notification_outbox.workspace_id)
          )
          OR (
            notification_outbox.workspace_id IS NULL
            AND notification_outbox.notification_id IS NULL
            AND notification_outbox.channel = 'email'
            AND EXISTS (
              SELECT 1
              FROM users AS account_user
              WHERE account_user.id = NULLIF(
                current_setting('app.user_id', true), ''
              )::uuid
                AND account_user.email = notification_outbox.recipient
            )
          )
        )
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _install_predicates()
    _install_workspace_policies()
    _install_membership_policies()
    _install_account_outbox_policies()


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        "DROP POLICY IF EXISTS api_outbox_insert_policy ON notification_outbox"
    )
    op.execute(
        "DROP POLICY IF EXISTS api_outbox_select_policy ON notification_outbox"
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

    for policy in (
        "admin_membership_select_policy",
        "worker_membership_select_policy",
        "api_membership_delete_policy",
        "api_membership_update_policy",
        "api_membership_insert_policy",
        "api_membership_select_policy",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON memberships")
    op.execute("ALTER TABLE memberships DISABLE ROW LEVEL SECURITY")
    op.execute(f"GRANT UPDATE ON TABLE memberships TO {API_ROLE}")

    for policy in (
        "admin_workspace_policy",
        "worker_workspace_select_policy",
        "api_workspace_update_policy",
        "api_workspace_insert_policy",
        "api_workspace_select_policy",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON workspaces")
    op.execute("ALTER TABLE workspaces DISABLE ROW LEVEL SECURITY")

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
              FROM memberships AS membership
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
    for signature in (
        "app_membership_new_role_allowed(uuid, text)",
        "app_membership_existing_manage_allowed(uuid, text)",
        "app_membership_insert_allowed(uuid, uuid, text)",
        "app_workspace_is_unclaimed(uuid)",
        "app_user_has_valid_invitation(uuid)",
        "app_user_workspace_role(uuid)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
