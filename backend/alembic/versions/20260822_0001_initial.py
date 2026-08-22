"""Deterministic baseline for the multi-tenant platform.

Revision ID: 20260822_0001
Revises: None

This revision intentionally contains a frozen schema snapshot.  Importing the
live ORM metadata here would make an old migration change whenever a model is
edited and can cause later revisions to add the same column twice.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260822_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("normalized_email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False),
        sa.Column("totp_secret_encrypted", sa.Text()),
        sa.Column("totp_pending_secret_encrypted", sa.Text()),
        sa.Column("totp_recovery_hashes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint(
            "normalized_email", name=op.f("uq_users_normalized_email")
        ),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("profile_quota", sa.Integer(), nullable=False),
        sa.Column("member_quota", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspaces_slug")),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("actor_user_id", sa.Uuid()),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("entity_type", sa.String(80)),
        sa.Column("entity_id", sa.String(100)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_audit_events_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_workspace_created",
        "audit_events",
        ["workspace_id", "created_at"],
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("ip_address", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
    )
    op.create_index(op.f("ix_auth_sessions_expires_at"), "auth_sessions", ["expires_at"])
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"])
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "admin", "operator", "viewer", name="workspacerole", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_memberships_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name=op.f("uq_memberships_workspace_id")
        ),
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"])
    op.create_index(
        op.f("ix_memberships_workspace_id"), "memberships", ["workspace_id"]
    )
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("in_app_enabled", sa.Boolean(), nullable=False),
        sa.Column("push_enabled", sa.Boolean(), nullable=False),
        sa.Column("email_enabled", sa.Boolean(), nullable=False),
        sa.Column("success_notifications", sa.Boolean(), nullable=False),
        sa.Column("telegram_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_notification_preferences_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            name=op.f("uq_notification_preferences_workspace_id"),
        ),
    )
    op.create_index(
        op.f("ix_notification_preferences_user_id"),
        "notification_preferences",
        ["user_id"],
    )
    op.create_index(
        op.f("ix_notification_preferences_workspace_id"),
        "notification_preferences",
        ["workspace_id"],
    )
    op.create_table(
        "one_time_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "verify_email", "recover_password", name="tokenpurpose", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_one_time_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_one_time_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_one_time_tokens_token_hash")),
    )
    op.create_index(op.f("ix_one_time_tokens_user_id"), "one_time_tokens", ["user_id"])
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum(
                "churchtools", "worshiptools", name="providertype", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("base_url", sa.String(512)),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("credentials_encrypted", sa.Text()),
        sa.Column("credentials_configured", sa.Boolean(), nullable=False),
        sa.Column("credential_hint", sa.String(120)),
        sa.Column("encryption_key_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True)),
        sa.Column("last_test_succeeded", sa.Boolean()),
        sa.Column("last_test_message", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_provider_connections_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_connections")),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "name",
            name=op.f("uq_provider_connections_workspace_id"),
        ),
    )
    op.create_index(
        op.f("ix_provider_connections_workspace_id"),
        "provider_connections",
        ["workspace_id"],
    )
    op.create_index(
        "ix_provider_connections_workspace_provider",
        "provider_connections",
        ["workspace_id", "provider"],
    )
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_hash", sa.String(64), nullable=False),
        sa.Column("subscription_encrypted", sa.Text(), nullable=False),
        sa.Column("device_name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_push_subscriptions_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_push_subscriptions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_subscriptions")),
        sa.UniqueConstraint(
            "workspace_id", "endpoint_hash", name=op.f("uq_push_subscriptions_workspace_id")
        ),
    )
    op.create_index(
        op.f("ix_push_subscriptions_user_id"), "push_subscriptions", ["user_id"]
    )
    op.create_index(
        op.f("ix_push_subscriptions_workspace_id"),
        "push_subscriptions",
        ["workspace_id"],
    )
    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("normalized_email", sa.String(320), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "admin", "operator", "viewer", name="workspacerole", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_invitations_invited_by_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_invitations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_workspace_invitations_token_hash")),
        sa.UniqueConstraint(
            "workspace_id",
            "normalized_email",
            name=op.f("uq_workspace_invitations_workspace_id"),
        ),
    )
    op.create_index(
        op.f("ix_workspace_invitations_workspace_id"),
        "workspace_invitations",
        ["workspace_id"],
    )
    op.create_table(
        "sync_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_connection_id", sa.Uuid(), nullable=False),
        sa.Column("target_connection_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("match_mode", sa.String(32), nullable=False),
        sa.Column("source_timezone", sa.String(64), nullable=False),
        sa.Column("target_timezone", sa.String(64), nullable=False),
        sa.Column("lookahead_days", sa.Integer(), nullable=False),
        sa.Column("schedule_type", sa.String(16), nullable=False),
        sa.Column("interval_minutes", sa.Integer()),
        sa.Column("cron_expression", sa.String(120)),
        sa.Column("event_rules", sa.JSON(), nullable=False),
        sa.Column("placements", sa.JSON(), nullable=False),
        sa.Column("notification_preferences", sa.JSON(), nullable=False),
        sa.Column("create_missing_songs", sa.Boolean(), nullable=False),
        sa.Column("song_category_id", sa.Integer()),
        sa.Column("arrangement_name", sa.String(50), nullable=False),
        sa.Column("agenda_item_defaults", sa.JSON(), nullable=False),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_connection_id"],
            ["provider_connections.id"],
            name=op.f("fk_sync_profiles_source_connection_id_provider_connections"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_connection_id"],
            ["provider_connections.id"],
            name=op.f("fk_sync_profiles_target_connection_id_provider_connections"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_sync_profiles_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_profiles")),
        sa.UniqueConstraint(
            "workspace_id", "name", name=op.f("uq_sync_profiles_workspace_id")
        ),
    )
    op.create_index(
        op.f("ix_sync_profiles_workspace_id"), "sync_profiles", ["workspace_id"]
    )
    op.create_table(
        "remote_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("target_connection_id", sa.Uuid(), nullable=False),
        sa.Column("target_event_id", sa.String(200), nullable=False),
        sa.Column("agenda_item_id", sa.String(200), nullable=False),
        sa.Column("source_key", sa.String(300), nullable=False),
        sa.Column("placement_id", sa.String(200), nullable=False),
        sa.Column("fingerprint_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["sync_profiles.id"],
            name=op.f("fk_remote_bindings_profile_id_sync_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_connection_id"],
            ["provider_connections.id"],
            name=op.f("fk_remote_bindings_target_connection_id_provider_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_remote_bindings_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_remote_bindings")),
        sa.UniqueConstraint(
            "target_connection_id",
            "target_event_id",
            "agenda_item_id",
            name=op.f("uq_remote_bindings_target_connection_id"),
        ),
    )
    op.create_index(
        "ix_remote_bindings_profile_event",
        "remote_bindings",
        ["profile_id", "target_event_id"],
    )
    op.create_index(
        op.f("ix_remote_bindings_workspace_id"), "remote_bindings", ["workspace_id"]
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "partial",
                "failed",
                "canceled",
                "skipped",
                name="syncrunstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            sa.Enum("scheduled", "manual", "recovery", name="synctrigger", native_enum=False),
            nullable=False,
        ),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("plan_json", sa.JSON()),
        sa.Column("error_json", sa.JSON()),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("notifications_fanned_out_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["sync_profiles.id"],
            name=op.f("fk_sync_runs_profile_id_sync_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_sync_runs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(
        "ix_sync_runs_profile_status", "sync_runs", ["profile_id", "status"]
    )
    op.create_index(
        "ix_sync_runs_workspace_created", "sync_runs", ["workspace_id", "created_at"]
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("run_id", sa.Uuid()),
        sa.Column(
            "severity",
            sa.Enum("info", "warning", "error", name="notificationseverity", native_enum=False),
            nullable=False,
        ),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("deduplication_key", sa.String(200)),
        sa.ForeignKeyConstraint(
            ["run_id"], ["sync_runs.id"], name=op.f("fk_notifications_run_id_sync_runs"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_notifications_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_notifications_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint(
            "deduplication_key", name=op.f("uq_notifications_deduplication_key")
        ),
    )
    op.create_index(
        "ix_notifications_workspace_created",
        "notifications",
        ["workspace_id", "created_at"],
    )
    op.create_table(
        "sync_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(200)),
        sa.Column("source_id", sa.String(200)),
        sa.Column("target_id", sa.String(200)),
        sa.Column(
            "kind",
            sa.Enum(
                "create_song",
                "create_arrangement",
                "insert_item",
                "replace_item",
                "delete_owned_item",
                "noop",
                name="syncactionkind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "planned",
                "applied",
                "verified",
                "failed",
                "skipped",
                name="syncactionstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("fingerprint_json", sa.JSON()),
        sa.Column("error_json", sa.JSON()),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["run_id"], ["sync_runs.id"], name=op.f("fk_sync_actions_run_id_sync_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_actions")),
        sa.UniqueConstraint("run_id", "ordinal", name=op.f("uq_sync_actions_run_id")),
    )
    op.create_index(op.f("ix_sync_actions_run_id"), "sync_actions", ["run_id"])
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("notification_id", sa.Uuid()),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "processing", "delivered", "failed", "dead", name="outboxstatus", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(100)),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_outbox_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_notification_outbox_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_outbox")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_notification_outbox_idempotency_key")
        ),
    )
    op.create_index(
        "ix_notification_outbox_due",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        op.f("ix_notification_outbox_notification_id"),
        "notification_outbox",
        ["notification_id"],
    )
    op.create_index(
        op.f("ix_notification_outbox_workspace_id"),
        "notification_outbox",
        ["workspace_id"],
    )

    if op.get_bind().dialect.name in {"postgresql", "sqlite"}:
        op.execute(
            "CREATE UNIQUE INDEX uq_sync_runs_one_active_per_profile "
            "ON sync_runs (profile_id) WHERE status IN ('queued', 'running')"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name in {"postgresql", "sqlite"}:
        op.execute("DROP INDEX IF EXISTS uq_sync_runs_one_active_per_profile")
    op.drop_index(
        op.f("ix_notification_outbox_workspace_id"),
        table_name="notification_outbox",
    )
    op.drop_index(
        op.f("ix_notification_outbox_notification_id"),
        table_name="notification_outbox",
    )
    op.drop_index("ix_notification_outbox_due", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index(op.f("ix_sync_actions_run_id"), table_name="sync_actions")
    op.drop_table("sync_actions")
    op.drop_index("ix_notifications_workspace_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_sync_runs_workspace_created", table_name="sync_runs")
    op.drop_index("ix_sync_runs_profile_status", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index(op.f("ix_remote_bindings_workspace_id"), table_name="remote_bindings")
    op.drop_index("ix_remote_bindings_profile_event", table_name="remote_bindings")
    op.drop_table("remote_bindings")
    op.drop_index(op.f("ix_sync_profiles_workspace_id"), table_name="sync_profiles")
    op.drop_table("sync_profiles")
    op.drop_index(
        op.f("ix_workspace_invitations_workspace_id"),
        table_name="workspace_invitations",
    )
    op.drop_table("workspace_invitations")
    op.drop_index(
        op.f("ix_push_subscriptions_workspace_id"), table_name="push_subscriptions"
    )
    op.drop_index(
        op.f("ix_push_subscriptions_user_id"), table_name="push_subscriptions"
    )
    op.drop_table("push_subscriptions")
    op.drop_index(
        "ix_provider_connections_workspace_provider",
        table_name="provider_connections",
    )
    op.drop_index(
        op.f("ix_provider_connections_workspace_id"),
        table_name="provider_connections",
    )
    op.drop_table("provider_connections")
    op.drop_index(op.f("ix_one_time_tokens_user_id"), table_name="one_time_tokens")
    op.drop_table("one_time_tokens")
    op.drop_index(
        op.f("ix_notification_preferences_workspace_id"),
        table_name="notification_preferences",
    )
    op.drop_index(
        op.f("ix_notification_preferences_user_id"),
        table_name="notification_preferences",
    )
    op.drop_table("notification_preferences")
    op.drop_index(op.f("ix_memberships_workspace_id"), table_name="memberships")
    op.drop_index(op.f("ix_memberships_user_id"), table_name="memberships")
    op.drop_table("memberships")
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_expires_at"), table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_audit_events_workspace_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("workspaces")
    op.drop_table("users")
