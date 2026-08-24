from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    # UUIDv4 is used until the minimum supported Python exposes UUIDv7.
    return uuid.uuid4()


class StringEnum(str, enum.Enum):
    pass


class WorkspaceRole(StringEnum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class ProviderType(StringEnum):
    CHURCHTOOLS = "churchtools"
    WORSHIPTOOLS = "worshiptools"


class TokenPurpose(StringEnum):
    VERIFY_EMAIL = "verify_email"
    RECOVER_PASSWORD = "recover_password"


class SyncRunStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"


class SyncTrigger(StringEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    RECOVERY = "recovery"


class SyncActionKind(StringEnum):
    CREATE_SONG = "create_song"
    CREATE_ARRANGEMENT = "create_arrangement"
    INSERT_ITEM = "insert_item"
    REPLACE_ITEM = "replace_item"
    DELETE_OWNED_ITEM = "delete_owned_item"
    NOOP = "noop"


class SyncActionStatus(StringEnum):
    PLANNED = "planned"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotificationSeverity(StringEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class OutboxStatus(StringEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"


enum_values = lambda enum_cls: [item.value for item in enum_cls]  # noqa: E731


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_pending_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_recovery_hashes: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship()


class OneTimeToken(Base):
    __tablename__ = "one_time_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    purpose: Mapped[TokenPurpose] = mapped_column(
        SAEnum(TokenPurpose, values_callable=enum_values, native_enum=False),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_quota: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    member_quota: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        SAEnum(WorkspaceRole, values_callable=enum_values, native_enum=False),
        nullable=False,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class WorkspaceInvitation(Base):
    __tablename__ = "workspace_invitations"
    __table_args__ = (UniqueConstraint("workspace_id", "normalized_email"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[WorkspaceRole] = mapped_column(
        SAEnum(WorkspaceRole, values_callable=enum_values, native_enum=False),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderConnection(TimestampMixin, Base):
    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "name"),
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_provider_connections_workspace_id_id",
        ),
        Index("ix_provider_connections_workspace_provider", "workspace_id", "provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[ProviderType] = mapped_column(
        SAEnum(ProviderType, values_callable=enum_values, native_enum=False),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    credentials_configured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credential_hint: Mapped[str | None] = mapped_column(String(120))
    encryption_key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_succeeded: Mapped[bool | None] = mapped_column(Boolean)
    last_test_message: Mapped[str | None] = mapped_column(String(500))


class SyncProfile(TimestampMixin, Base):
    __tablename__ = "sync_profiles"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_sync_profiles_workspace_id_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_connection_id"],
            ["provider_connections.workspace_id", "provider_connections.id"],
            name="fk_sync_profiles_workspace_source_connection",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "target_connection_id"],
            ["provider_connections.workspace_id", "provider_connections.id"],
            name="fk_sync_profiles_workspace_target_connection",
            ondelete="RESTRICT",
        ),
        Index("ix_sync_profiles_due", "enabled", "next_scheduled_at"),
        # The database owns the new-column default. Avoid RETURNING it so an
        # insert also remains safe during the schema-upgrade window.
        {"implicit_returning": False},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sync_mode: Mapped[str] = mapped_column(
        String(32), server_default="source_changes_only", nullable=False
    )
    match_mode: Mapped[str] = mapped_column(String(32), default="exact_time", nullable=False)
    source_timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    target_timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    lookahead_days: Mapped[int] = mapped_column(Integer, default=28, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), default="interval", nullable=False)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, default=60)
    cron_expression: Mapped[str | None] = mapped_column(String(120))
    event_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    placements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    notification_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    create_missing_songs: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    song_category_id: Mapped[int | None] = mapped_column(Integer)
    arrangement_name: Mapped[str] = mapped_column(
        String(50), default="Standard-Arrangement", nullable=False
    )
    agenda_item_defaults: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class EventSyncState(Base):
    """Last successfully verified WT input for one matched event pair."""

    __tablename__ = "event_sync_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "profile_id"],
            ["sync_profiles.workspace_id", "sync_profiles.id"],
            name="fk_event_sync_states_workspace_profile",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "profile_id",
            "source_event_id",
            "target_event_id",
            name="uq_event_sync_states_profile_event_pair",
        ),
        Index("ix_event_sync_states_workspace_profile", "workspace_id", "profile_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    target_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "profile_id"],
            ["sync_profiles.workspace_id", "sync_profiles.id"],
            name="fk_sync_runs_workspace_profile",
            ondelete="CASCADE",
        ),
        Index("ix_sync_runs_workspace_created", "workspace_id", "created_at"),
        Index("ix_sync_runs_profile_status", "profile_id", "status"),
        Index(
            "ix_sync_runs_dispatch_recovery",
            "status",
            "dispatch_attempted_at",
            "lease_expires_at",
        ),
        Index(
            "ix_sync_runs_notification_fanout",
            "status",
            postgresql_where=text("notifications_fanned_out_at IS NULL"),
            sqlite_where=text("notifications_fanned_out_at IS NULL"),
        ),
        Index(
            "uq_sync_runs_one_active_per_profile",
            "profile_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SyncRunStatus] = mapped_column(
        SAEnum(SyncRunStatus, values_callable=enum_values, native_enum=False),
        default=SyncRunStatus.QUEUED,
        nullable=False,
    )
    trigger: Mapped[SyncTrigger] = mapped_column(
        SAEnum(SyncTrigger, values_callable=enum_values, native_enum=False),
        nullable=False,
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    notifications_fanned_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    actions: Mapped[list["SyncAction"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="SyncAction.ordinal",
    )


class SyncAction(Base):
    __tablename__ = "sync_actions"
    __table_args__ = (UniqueConstraint("run_id", "ordinal"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_id: Mapped[str | None] = mapped_column(String(200))
    source_id: Mapped[str | None] = mapped_column(String(200))
    target_id: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[SyncActionKind] = mapped_column(
        SAEnum(SyncActionKind, values_callable=enum_values, native_enum=False),
        nullable=False,
    )
    status: Mapped[SyncActionStatus] = mapped_column(
        SAEnum(SyncActionStatus, values_callable=enum_values, native_enum=False),
        default=SyncActionStatus.PLANNED,
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    fingerprint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    planned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[SyncRun] = relationship(back_populates="actions")


class RemoteBinding(TimestampMixin, Base):
    """Persistent ownership marker for safely removable agenda items."""

    __tablename__ = "remote_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "profile_id"],
            ["sync_profiles.workspace_id", "sync_profiles.id"],
            name="fk_remote_bindings_workspace_profile",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "target_connection_id"],
            ["provider_connections.workspace_id", "provider_connections.id"],
            name="fk_remote_bindings_workspace_target_connection",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "target_connection_id", "target_event_id", "agenda_item_id"
        ),
        Index("ix_remote_bindings_profile_event", "profile_id", "target_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    agenda_item_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_key: Mapped[str] = mapped_column(String(300), nullable=False)
    placement_id: Mapped[str] = mapped_column(String(200), nullable=False)
    fingerprint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    severity: Mapped[NotificationSeverity] = mapped_column(
        SAEnum(NotificationSeverity, values_callable=enum_values, native_enum=False),
        default=NotificationSeverity.INFO,
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deduplication_key: Mapped[str | None] = mapped_column(String(200), unique=True)


class NotificationPreference(TimestampMixin, Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    success_notifications: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (UniqueConstraint("workspace_id", "endpoint_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    endpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    subscription_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    device_name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (Index("ix_notification_outbox_due", "status", "next_attempt_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    payload_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        SAEnum(OutboxStatus, values_callable=enum_values, native_enum=False),
        default=OutboxStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    claim_token: Mapped[str | None] = mapped_column(String(64))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
