"""Allow invitation recipients to select their own open token row.

Revision ID: 20260822_0005
Revises: 20260822_0004
"""

from __future__ import annotations

from alembic import op


revision = "20260822_0005"
down_revision = "20260822_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # The generic tenant policy intentionally requires an existing workspace
    # membership. This narrowly scoped SELECT-only policy lets an authenticated
    # recipient discover exactly their own, still-open invitation by token. It
    # grants no UPDATE right; acceptance must insert the membership first.
    op.execute(
        """
        CREATE POLICY invitation_recipient_select_policy
        ON workspace_invitations
        FOR SELECT
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
    # PostgreSQL applies UPDATE policies to SELECT ... FOR UPDATE as well. A
    # matching USING policy is therefore required to lock the token row. Its
    # WITH CHECK deliberately still requires tenant membership, so recipients
    # cannot update an invitation until acceptance has flushed the membership.
    op.execute(
        """
        CREATE POLICY invitation_recipient_lock_policy
        ON workspace_invitations
        FOR UPDATE
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


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "DROP POLICY IF EXISTS invitation_recipient_lock_policy "
        "ON workspace_invitations"
    )
    op.execute(
        "DROP POLICY IF EXISTS invitation_recipient_select_policy "
        "ON workspace_invitations"
    )
