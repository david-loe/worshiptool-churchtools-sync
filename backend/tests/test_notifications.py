from __future__ import annotations

from datetime import datetime, timezone

from fastapi.routing import APIRoute
from sqlalchemy import select

from app.dependencies import WorkspaceAccess, require_csrf
from app.main import create_app
from app.models import (
    Membership,
    Notification,
    NotificationPreference,
    NotificationSeverity,
    User,
    Workspace,
    WorkspaceRole,
)
from app.routers.notifications import (
    get_preferences,
    list_notifications,
    mark_all_read,
    router as notifications_router,
    update_preferences,
)
from app.schemas import NotificationPreferenceUpdate


def _user(email: str) -> User:
    return User(
        email=email,
        normalized_email=email.casefold(),
        password_hash="not-used-by-this-test",
        email_verified_at=datetime.now(timezone.utc),
    )


def _notification(
    workspace: Workspace, user: User | None, title: str
) -> Notification:
    return Notification(
        workspace_id=workspace.id,
        user_id=user.id if user is not None else None,
        severity=NotificationSeverity.INFO,
        category="test",
        title=title,
        body="Testbenachrichtigung",
        data_json={},
    )


def test_mark_all_read_updates_every_visible_unread_row_without_page_limit(db):
    current_user = _user("current@example.org")
    other_user = _user("other@example.org")
    workspace = Workspace(name="Aktuell", slug="aktuell")
    other_workspace = Workspace(name="Fremd", slug="fremd")
    db.add_all([current_user, other_user, workspace, other_workspace])
    db.flush()
    db.add_all(
        [
            Membership(
                workspace_id=workspace.id,
                user_id=current_user.id,
                role=WorkspaceRole.OWNER,
            ),
            Membership(
                workspace_id=workspace.id,
                user_id=other_user.id,
                role=WorkspaceRole.VIEWER,
            ),
            Membership(
                workspace_id=other_workspace.id,
                user_id=current_user.id,
                role=WorkspaceRole.OWNER,
            ),
        ]
    )

    own_unread = [
        _notification(workspace, current_user, f"Aktuell {index}")
        for index in range(130)
    ]
    already_read_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    own_already_read = _notification(workspace, current_user, "Schon gelesen")
    own_already_read.read_at = already_read_at
    other_user_notification = _notification(workspace, other_user, "Anderer Nutzer")
    broadcast_notification = _notification(workspace, None, "Nicht zugeordnet")
    other_workspace_notification = _notification(
        other_workspace, current_user, "Anderer Workspace"
    )
    db.add_all(
        [
            *own_unread,
            own_already_read,
            other_user_notification,
            broadcast_notification,
            other_workspace_notification,
        ]
    )
    db.commit()

    access = WorkspaceAccess(workspace, current_user, WorkspaceRole.OWNER)
    first_page = list_notifications(
        access, db, unread_only=True, limit=100, offset=0
    )
    older_page = list_notifications(
        access, db, unread_only=True, limit=100, offset=100
    )

    assert first_page.total == 130
    assert first_page.unread == 130
    assert len(first_page.items) == 100
    assert len(older_page.items) == 30

    result = mark_all_read(access, db, None)

    assert result.updated == 130
    assert result.read_at.tzinfo is not None
    # The endpoint intentionally skips ORM synchronization for a single,
    # constant-cost bulk UPDATE. Reload rows before inspecting persisted state.
    db.expire_all()
    assert all(
        notification.read_at is not None
        for notification in db.scalars(
            select(Notification).where(
                Notification.workspace_id == workspace.id,
                Notification.user_id == current_user.id,
            )
        )
        if notification.id != own_already_read.id
    )
    db.refresh(own_already_read)
    db.refresh(other_user_notification)
    db.refresh(broadcast_notification)
    db.refresh(other_workspace_notification)
    persisted_read_at = own_already_read.read_at
    assert persisted_read_at is not None
    if persisted_read_at.tzinfo is None:
        persisted_read_at = persisted_read_at.replace(tzinfo=timezone.utc)
    assert persisted_read_at == already_read_at
    assert other_user_notification.read_at is None
    assert broadcast_notification.read_at is None
    assert other_workspace_notification.read_at is None

    unread_page = list_notifications(
        access, db, unread_only=True, limit=100, offset=0
    )
    assert unread_page.total == 0
    assert unread_page.unread == 0
    assert unread_page.items == []


def test_mark_all_read_openapi_contract_and_csrf_dependency(settings, database):
    app = create_app(settings, database=database)
    path = "/api/v1/workspaces/{workspace_id}/notifications/read-all"
    route = next(
        route
        for route in notifications_router.routes
        if isinstance(route, APIRoute)
        and route.path == "/workspaces/{workspace_id}/notifications/read-all"
        and "POST" in route.methods
    )

    assert any(dependency.call is require_csrf for dependency in route.dependant.dependencies)

    operation = app.openapi()["paths"][path]["post"]
    response_schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema["$ref"].endswith("/NotificationMarkAllReadResponse")
    assert "requestBody" not in operation
    assert "application/problem+json" in operation["responses"]["default"]["content"]


def test_notification_preferences_default_and_update_contract(db):
    current_user = _user("preferences@example.org")
    workspace = Workspace(name="Präferenzen", slug="praeferenzen")
    db.add_all([current_user, workspace])
    db.flush()
    access = WorkspaceAccess(workspace, current_user, WorkspaceRole.OWNER)

    defaults = get_preferences(access, db)
    assert defaults.model_dump() == {
        "push_enabled": False,
        "email_enabled": True,
        "success_notifications": False,
        "failure_notifications": True,
        "new_song_notifications": True,
    }

    updated = update_preferences(
        NotificationPreferenceUpdate(
            push_enabled=True,
            email_enabled=False,
            success_notifications=True,
            failure_notifications=False,
            new_song_notifications=False,
        ),
        access,
        db,
        None,
    )

    assert isinstance(updated, NotificationPreference)
    assert updated.push_enabled is True
    assert updated.email_enabled is False
    assert updated.success_notifications is True
    assert updated.failure_notifications is False
    assert updated.new_song_notifications is False
