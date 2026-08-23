from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select, update
from sqlalchemy.sql.elements import ColumnElement

from ..dependencies import CsrfDep, DbDep, SettingsDep, WorkspaceAccessDep
from ..models import Notification, NotificationPreference, PushSubscription, User
from ..problems import ProblemException
from ..schemas import (
    NotificationList,
    NotificationMarkAllReadResponse,
    NotificationOut,
    NotificationPreferenceOut,
    NotificationPreferenceUpdate,
    PushSubscriptionCreate,
    PushSubscriptionOut,
)
from ..security import SecretCipher
from ..web_push import PushEndpointError, validate_push_endpoint


router = APIRouter(
    prefix="/workspaces/{workspace_id}/notifications", tags=["Benachrichtigungen"]
)


def _visible_notifications(
    access: WorkspaceAccessDep,
) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (
        Notification.workspace_id == access.workspace.id,
        Notification.user_id == access.user.id,
    )


@router.get("", response_model=NotificationList)
def list_notifications(
    access: WorkspaceAccessDep,
    db: DbDep,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> NotificationList:
    visible = _visible_notifications(access)
    predicates = list(visible)
    if unread_only:
        predicates.append(Notification.read_at.is_(None))
    total = db.scalar(select(func.count()).select_from(Notification).where(*predicates)) or 0
    unread = db.scalar(
        select(func.count()).select_from(Notification).where(
            *visible, Notification.read_at.is_(None)
        )
    ) or 0
    items = db.scalars(
        select(Notification)
        .where(*predicates)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return NotificationList(
        items=items, total=total, unread=unread, limit=limit, offset=offset
    )


@router.post("/read-all", response_model=NotificationMarkAllReadResponse)
def mark_all_read(
    access: WorkspaceAccessDep,
    db: DbDep,
    csrf: CsrfDep,
) -> NotificationMarkAllReadResponse:
    read_at = datetime.now(timezone.utc)
    result = db.execute(
        update(Notification)
        .where(*_visible_notifications(access), Notification.read_at.is_(None))
        .values(read_at=read_at)
        .execution_options(synchronize_session=False)
    )
    updated = result.rowcount if result.rowcount is not None else 0
    db.commit()
    return NotificationMarkAllReadResponse(
        updated=max(0, updated),
        read_at=read_at,
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: uuid.UUID,
    access: WorkspaceAccessDep,
    db: DbDep,
    csrf: CsrfDep,
) -> Notification:
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            *_visible_notifications(access),
        )
    )
    if notification is None:
        raise ProblemException(404, "Nicht gefunden", "Benachrichtigung nicht gefunden.", "not_found")
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        db.commit()
    return notification


def _preference(access: WorkspaceAccessDep, db: DbDep) -> NotificationPreference | None:
    return db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.workspace_id == access.workspace.id,
            NotificationPreference.user_id == access.user.id,
        )
    )


@router.get("/preferences", response_model=NotificationPreferenceOut)
def get_preferences(
    access: WorkspaceAccessDep, db: DbDep
) -> NotificationPreferenceOut:
    preference = _preference(access, db)
    if preference is None:
        return NotificationPreferenceOut(
            in_app_enabled=True,
            push_enabled=False,
            email_enabled=True,
            success_notifications=False,
            telegram_enabled=False,
        )
    return NotificationPreferenceOut.model_validate(preference)


@router.put("/preferences", response_model=NotificationPreferenceOut)
def update_preferences(
    payload: NotificationPreferenceUpdate,
    access: WorkspaceAccessDep,
    db: DbDep,
    csrf: CsrfDep,
) -> NotificationPreference:
    preference = _preference(access, db)
    if preference is None:
        preference = NotificationPreference(
            workspace_id=access.workspace.id,
            user_id=access.user.id,
        )
        db.add(preference)
    for key, value in payload.model_dump().items():
        setattr(preference, key, value)
    db.commit()
    return preference


@router.get("/push-subscriptions", response_model=list[PushSubscriptionOut])
def list_push_subscriptions(
    access: WorkspaceAccessDep, db: DbDep
) -> list[PushSubscription]:
    return list(
        db.scalars(
            select(PushSubscription)
            .where(
                PushSubscription.workspace_id == access.workspace.id,
                PushSubscription.user_id == access.user.id,
                PushSubscription.revoked_at.is_(None),
            )
            .order_by(PushSubscription.created_at.desc())
        ).all()
    )


@router.post("/push-subscriptions", response_model=PushSubscriptionOut, status_code=201)
def register_push_subscription(
    payload: PushSubscriptionCreate,
    access: WorkspaceAccessDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> PushSubscription:
    try:
        endpoint = validate_push_endpoint(
            payload.endpoint, settings.web_push_allowed_host_suffixes
        )
    except PushEndpointError as exc:
        raise ProblemException(
            422,
            "Push-Endpunkt nicht erlaubt",
            "Web-Push-Endpunkte müssen über HTTPS:443 einen erlaubten Push-Dienst verwenden.",
            "push_endpoint_not_allowed",
        ) from exc
    endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    # Serialize quota checks across all sessions of this user. Locking the
    # membership itself is not safe here: PostgreSQL applies its UPDATE RLS
    # policy to SELECT FOR UPDATE, hiding viewer/operator memberships and the
    # sole owner's membership even though WorkspaceAccess already authorized
    # the request.
    user_id = db.scalar(
        select(User.id).where(User.id == access.user.id).with_for_update()
    )
    if user_id is None:
        raise ProblemException(
            401,
            "Nicht angemeldet",
            "Für diese Anfrage ist eine Anmeldung erforderlich.",
            "authentication_required",
        )
    subscription = db.scalar(
        select(PushSubscription).where(
            PushSubscription.workspace_id == access.workspace.id,
            PushSubscription.endpoint_hash == endpoint_hash,
        )
    )
    consumes_new_slot = (
        subscription is None
        or subscription.user_id != access.user.id
        or subscription.revoked_at is not None
    )
    if consumes_new_slot:
        active_count = db.scalar(
            select(func.count())
            .select_from(PushSubscription)
            .where(
                PushSubscription.workspace_id == access.workspace.id,
                PushSubscription.user_id == access.user.id,
                PushSubscription.revoked_at.is_(None),
            )
        ) or 0
        if active_count >= settings.max_push_subscriptions_per_user_workspace:
            raise ProblemException(
                409,
                "Push-Geräte-Limit erreicht",
                "Entferne zuerst eine bestehende Push-Registrierung.",
                "push_subscription_quota_exceeded",
            )
    if subscription is None:
        subscription = PushSubscription(
            workspace_id=access.workspace.id,
            user_id=access.user.id,
            endpoint_hash=endpoint_hash,
            subscription_encrypted="pending",
            device_name=payload.device_name,
        )
        db.add(subscription)
        db.flush()
    subscription.user_id = access.user.id
    subscription.device_name = payload.device_name
    subscription.revoked_at = None
    subscription.subscription_encrypted = SecretCipher(settings).encrypt_json(
        {
            "endpoint": endpoint,
            "keys": {"p256dh": payload.p256dh, "auth": payload.auth},
        },
        context=f"push-subscription:{subscription.id}",
    )
    db.commit()
    return subscription


@router.delete("/push-subscriptions/{subscription_id}", status_code=204)
def revoke_push_subscription(
    subscription_id: uuid.UUID,
    access: WorkspaceAccessDep,
    db: DbDep,
    csrf: CsrfDep,
) -> None:
    subscription = db.scalar(
        select(PushSubscription).where(
            PushSubscription.id == subscription_id,
            PushSubscription.workspace_id == access.workspace.id,
            PushSubscription.user_id == access.user.id,
        )
    )
    if subscription is None:
        raise ProblemException(404, "Nicht gefunden", "Push-Gerät nicht gefunden.", "not_found")
    subscription.revoked_at = datetime.now(timezone.utc)
    db.commit()
