"""Centralize notification preferences per workspace user.

Revision ID: 20260827_0016
Revises: 20260827_0015
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260827_0016"
down_revision = "20260827_0015"
branch_labels = None
depends_on = None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _uuid_value(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _profile_policy_union(connection: sa.Connection) -> dict[Any, dict[str, bool]]:
    rows = connection.execute(
        sa.text(
            "SELECT workspace_id, notification_preferences "
            "FROM sync_profiles"
        )
    ).mappings()
    policies: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        policies[row["workspace_id"]].append(
            _json_object(row["notification_preferences"])
        )

    defaults = {
        "email": True,
        "web_push": True,
        "notify_success": False,
        "notify_new_songs": True,
    }
    return {
        workspace_id: {
            key: any(bool(policy.get(key, default)) for policy in workspace_policies)
            for key, default in defaults.items()
        }
        for workspace_id, workspace_policies in policies.items()
    }


def _migrate_preferences(connection: sa.Connection) -> None:
    preference_table = sa.table(
        "notification_preferences",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("push_enabled", sa.Boolean()),
        sa.column("email_enabled", sa.Boolean()),
        sa.column("success_notifications", sa.Boolean()),
        sa.column("failure_notifications", sa.Boolean()),
        sa.column("new_song_notifications", sa.Boolean()),
        sa.column("in_app_enabled", sa.Boolean()),
        sa.column("telegram_enabled", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    policies = _profile_policy_union(connection)
    existing_rows = connection.execute(
        sa.text(
            "SELECT id, workspace_id, user_id, email_enabled, push_enabled, "
            "success_notifications FROM notification_preferences"
        )
    ).mappings()
    existing = {
        (row["workspace_id"], row["user_id"]): row for row in existing_rows
    }
    memberships = connection.execute(
        sa.text("SELECT workspace_id, user_id FROM memberships")
    ).mappings()
    now = datetime.now(timezone.utc)

    for membership in memberships:
        workspace_id = membership["workspace_id"]
        user_id = membership["user_id"]
        profile_policy = policies.get(workspace_id)
        current = existing.get((workspace_id, user_id))
        if current is None and profile_policy is None:
            continue

        email_enabled = bool(current["email_enabled"]) if current else True
        push_enabled = bool(current["push_enabled"]) if current else False
        success_notifications = (
            bool(current["success_notifications"]) if current else False
        )
        new_song_notifications = True
        if profile_policy is not None:
            email_enabled = email_enabled and profile_policy["email"]
            push_enabled = push_enabled and profile_policy["web_push"]
            success_notifications = (
                success_notifications and profile_policy["notify_success"]
            )
            new_song_notifications = profile_policy["notify_new_songs"]

        values = {
            "email_enabled": email_enabled,
            "push_enabled": push_enabled,
            "success_notifications": success_notifications,
            "failure_notifications": True,
            "new_song_notifications": new_song_notifications,
        }
        if current is not None:
            connection.execute(
                preference_table.update()
                .where(preference_table.c.id == _uuid_value(current["id"]))
                .values(**values, updated_at=now)
            )
        else:
            connection.execute(
                preference_table.insert().values(
                    **values,
                    id=uuid.uuid4(),
                    workspace_id=_uuid_value(workspace_id),
                    user_id=_uuid_value(user_id),
                    in_app_enabled=True,
                    telegram_enabled=False,
                    created_at=now,
                    updated_at=now,
                )
            )


def upgrade() -> None:
    with op.batch_alter_table("notification_preferences") as batch_op:
        batch_op.add_column(
            sa.Column(
                "failure_notifications",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "new_song_notifications",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )

    connection = op.get_bind()
    _migrate_preferences(connection)
    connection.execute(
        sa.text("DELETE FROM notification_outbox WHERE channel = 'telegram'")
    )

    with op.batch_alter_table("notification_preferences") as batch_op:
        batch_op.drop_column("in_app_enabled")
        batch_op.drop_column("telegram_enabled")
        batch_op.alter_column("failure_notifications", server_default=None)
        batch_op.alter_column("new_song_notifications", server_default=None)
    with op.batch_alter_table("sync_profiles") as batch_op:
        batch_op.drop_column("notification_preferences")


def downgrade() -> None:
    with op.batch_alter_table("sync_profiles") as batch_op:
        batch_op.add_column(
            sa.Column(
                "notification_preferences",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    with op.batch_alter_table("notification_preferences") as batch_op:
        batch_op.add_column(
            sa.Column(
                "in_app_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "telegram_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    connection = op.get_bind()
    profile_table = sa.table(
        "sync_profiles",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("notification_preferences", sa.JSON()),
    )
    profiles = connection.execute(
        sa.text("SELECT id, workspace_id FROM sync_profiles")
    ).mappings()
    for profile in profiles:
        rows = connection.execute(
            sa.text(
                "SELECT email_enabled, push_enabled, success_notifications, "
                "new_song_notifications FROM notification_preferences "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": profile["workspace_id"]},
        ).mappings()
        preferences = list(rows)
        policy = (
            {
                "in_app": True,
                "email": any(bool(row["email_enabled"]) for row in preferences),
                "web_push": any(bool(row["push_enabled"]) for row in preferences),
                "telegram": False,
                "notify_success": any(
                    bool(row["success_notifications"]) for row in preferences
                ),
                "notify_new_songs": any(
                    bool(row["new_song_notifications"]) for row in preferences
                ),
            }
            if preferences
            else {
                "in_app": True,
                "email": True,
                "web_push": True,
                "telegram": False,
                "notify_success": False,
                "notify_new_songs": True,
            }
        )
        connection.execute(
            profile_table.update()
            .where(profile_table.c.id == _uuid_value(profile["id"]))
            .values(notification_preferences=policy)
        )

    with op.batch_alter_table("sync_profiles") as batch_op:
        batch_op.alter_column("notification_preferences", server_default=None)
    with op.batch_alter_table("notification_preferences") as batch_op:
        batch_op.drop_column("failure_notifications")
        batch_op.drop_column("new_song_notifications")
        batch_op.alter_column("in_app_enabled", server_default=None)
        batch_op.alter_column("telegram_enabled", server_default=None)
