from __future__ import annotations

import html
import json
import logging
import smtplib
import ssl
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Callable, Mapping, Protocol

from pywebpush import WebPushException, webpush
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .database import Database
from .models import (
    AuditEvent,
    AuthSession,
    Membership,
    Notification,
    NotificationOutbox,
    NotificationPreference,
    NotificationSeverity,
    OneTimeToken,
    OutboxStatus,
    PushSubscription,
    SyncAction,
    SyncActionKind,
    SyncActionStatus,
    SyncProfile,
    SyncRun,
    SyncRunStatus,
    User,
    WorkspaceInvitation,
)
from .run_results import build_run_result
from .security import SecretCipher, utcnow
from .web_push import PushEndpointError, validate_push_endpoint


logger = logging.getLogger(__name__)


def set_worker_context(db: Session) -> None:
    """Compatibility hook; PostgreSQL worker access is bound to ``current_user``.

    Keeping the hook avoids branching in SQLite tests and older call sites,
    while deliberately refusing to turn a caller-controlled GUC into an RLS
    bypass.
    """

    del db


class DeliveryError(Exception):
    def __init__(self, code: str, *, permanent: bool = False):
        super().__init__(code)
        self.code = code[:100]
        self.permanent = permanent


class EmailSender(Protocol):
    def send(self, recipient: str, payload: Mapping[str, Any]) -> None: ...


class PushSender(Protocol):
    def send(
        self, subscription: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> None: ...


class SmtpEmailSender:
    def __init__(
        self,
        settings: Settings,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP_SSL,
    ):
        self.settings = settings
        self.smtp_factory = smtp_factory
        self.smtp_ssl_factory = smtp_ssl_factory

    def send(self, recipient: str, payload: Mapping[str, Any]) -> None:
        if not self.settings.smtp_host:
            raise DeliveryError("smtp_not_configured")
        subject = str(payload.get("subject") or "Worship Sync")
        subject = " ".join(subject.splitlines())[:200]
        text_body = str(payload.get("text") or "")
        html_body = payload.get("html")
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = recipient
        message["Subject"] = subject
        try:
            message_id = uuid.UUID(str(payload.get("_message_id")))
        except (TypeError, ValueError):
            message_id = None
        if message_id is not None:
            from urllib.parse import urlsplit

            domain = urlsplit(self.settings.public_base_url).hostname or "worship-sync.invalid"
            message["Message-ID"] = f"<{message_id}@{domain}>"
        message.set_content(text_body)
        if html_body:
            message.add_alternative(str(html_body), subtype="html")
        try:
            if self.settings.smtp_implicit_tls:
                smtp_connection = self.smtp_ssl_factory(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=self.settings.smtp_timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                smtp_connection = self.smtp_factory(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=self.settings.smtp_timeout_seconds,
                )
            with smtp_connection as smtp:
                smtp.ehlo()
                if self.settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if self.settings.smtp_username:
                    if self.settings.smtp_password is None:
                        raise DeliveryError("smtp_password_missing")
                    smtp.login(
                        self.settings.smtp_username,
                        self.settings.smtp_password.get_secret_value(),
                    )
                smtp.send_message(message)
        except DeliveryError:
            raise
        except smtplib.SMTPRecipientsRefused as exc:
            raise DeliveryError("smtp_recipient_refused", permanent=True) from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise DeliveryError("smtp_authentication_failed") from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise DeliveryError("smtp_delivery_failed") from exc


class VapidPushSender:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(
        self, subscription: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> None:
        if self.settings.vapid_private_key is None or not self.settings.vapid_subject:
            raise DeliveryError("vapid_not_configured")
        try:
            safe_subscription = dict(subscription)
            safe_subscription["endpoint"] = validate_push_endpoint(
                str(subscription.get("endpoint") or ""),
                self.settings.web_push_allowed_host_suffixes,
            )
        except PushEndpointError as exc:
            # Existing rows from before endpoint validation are rejected too.
            raise DeliveryError("push_endpoint_not_allowed", permanent=True) from exc
        try:
            webpush(
                subscription_info=safe_subscription,
                data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                vapid_private_key=self.settings.vapid_private_key.get_secret_value(),
                vapid_claims={"sub": self.settings.vapid_subject},
                timeout=15.0,
                ttl=60 * 60,
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                raise DeliveryError("push_subscription_gone", permanent=True) from exc
            if status_code is not None and 400 <= status_code < 500 and status_code != 429:
                raise DeliveryError("push_request_rejected", permanent=True) from exc
            raise DeliveryError("push_delivery_failed") from exc
        except (OSError, TimeoutError, ValueError) as exc:
            raise DeliveryError("push_delivery_failed") from exc


def enqueue_outbox(
    db: Session,
    settings: Settings,
    *,
    channel: str,
    recipient: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    workspace_id: uuid.UUID | None = None,
    notification_id: uuid.UUID | None = None,
) -> NotificationOutbox:
    canonical_key = idempotency_key[:160]
    existing = db.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key == canonical_key
        )
    )
    if existing is not None:
        return existing
    outbox_id = uuid.uuid4()
    payload_document = dict(payload)
    if channel == "email":
        # A retry can still cross the SMTP/DB commit boundary. A stable RFC
        # Message-ID gives receiving infrastructure a deterministic dedupe key.
        payload_document["_message_id"] = str(outbox_id)
    item = NotificationOutbox(
        id=outbox_id,
        workspace_id=workspace_id,
        notification_id=notification_id,
        channel=channel,
        recipient=recipient,
        payload_encrypted=SecretCipher(settings).encrypt_json(
            payload_document, context=f"outbox:{outbox_id}"
        ),
        idempotency_key=canonical_key,
    )
    try:
        # Keep a concurrent duplicate from rolling back unrelated domain work
        # in the surrounding transaction.
        with db.begin_nested():
            db.add(item)
            db.flush()
        return item
    except IntegrityError:
        existing = db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key == canonical_key
            )
        )
        if existing is None:
            raise
        return existing


def enqueue_email(
    db: Session,
    settings: Settings,
    *,
    recipient: str,
    subject: str,
    text: str,
    idempotency_key: str,
    workspace_id: uuid.UUID | None = None,
    html_body: str | None = None,
) -> NotificationOutbox:
    return enqueue_outbox(
        db,
        settings,
        channel="email",
        recipient=recipient,
        payload={"subject": subject, "text": text, "html": html_body},
        idempotency_key=idempotency_key,
        workspace_id=workspace_id,
    )


def link_email_body(title: str, intro: str, link: str) -> tuple[str, str]:
    text_body = f"{title}\n\n{intro}\n\n{link}\n\nFalls du das nicht angefordert hast, ignoriere diese E-Mail."
    html_body = (
        f"<h1>{html.escape(title)}</h1><p>{html.escape(intro)}</p>"
        f'<p><a href="{html.escape(link, quote=True)}">Weiter</a></p>'
        "<p>Falls du das nicht angefordert hast, ignoriere diese E-Mail.</p>"
    )
    return text_body, html_body


class NotificationService:
    TERMINAL = (
        SyncRunStatus.SUCCEEDED,
        SyncRunStatus.PARTIAL,
        SyncRunStatus.FAILED,
        SyncRunStatus.CANCELED,
        SyncRunStatus.SKIPPED,
    )

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def preference(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> NotificationPreference | None:
        return self.db.scalar(
            select(NotificationPreference).where(
                NotificationPreference.workspace_id == workspace_id,
                NotificationPreference.user_id == user_id,
            )
        )

    def create_for_user(
        self,
        *,
        workspace_id: uuid.UUID,
        user: User,
        severity: NotificationSeverity,
        category: str,
        title: str,
        body: str,
        deduplication_key: str,
        run_id: uuid.UUID | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Notification:
        existing = self.db.scalar(
            select(Notification).where(
                Notification.deduplication_key == deduplication_key
            )
        )
        if existing is not None:
            return existing
        notification = Notification(
            workspace_id=workspace_id,
            user_id=user.id,
            run_id=run_id,
            severity=severity,
            category=category,
            title=title[:200],
            body=body,
            data_json=dict(data or {}),
            deduplication_key=deduplication_key[:200],
        )
        self.db.add(notification)
        self.db.flush()
        preference = self.preference(workspace_id, user.id)
        email_enabled = (
            preference.email_enabled if preference is not None else True
        )
        push_enabled = (
            preference.push_enabled if preference is not None else False
        )
        if email_enabled:
            enqueue_outbox(
                self.db,
                self.settings,
                channel="email",
                recipient=user.email,
                payload={"subject": title, "text": f"{title}\n\n{body}"},
                idempotency_key=f"notification:{notification.id}:email",
                workspace_id=workspace_id,
                notification_id=notification.id,
            )
        if push_enabled:
            subscriptions = self.db.scalars(
                select(PushSubscription).where(
                    PushSubscription.workspace_id == workspace_id,
                    PushSubscription.user_id == user.id,
                    PushSubscription.revoked_at.is_(None),
                )
            ).all()
            for subscription in subscriptions:
                event_plan_id = (
                    str(data.get("event_plan_id"))
                    if data and data.get("event_plan_id")
                    else None
                )
                target_url = (
                    f"/runs/{run_id}?workspace={workspace_id}&event={event_plan_id}"
                    if run_id and event_plan_id
                    else (
                        f"/runs/{run_id}?workspace={workspace_id}"
                        if run_id
                        else f"/notifications?workspace={workspace_id}"
                    )
                )
                enqueue_outbox(
                    self.db,
                    self.settings,
                    channel="push",
                    recipient=str(subscription.id),
                    payload={
                        "title": title,
                        "body": body,
                        "data": {
                            "notification_id": str(notification.id),
                            "workspace_id": str(workspace_id),
                            "run_id": str(run_id) if run_id else None,
                            "event_plan_id": event_plan_id,
                            "url": target_url,
                        },
                    },
                    idempotency_key=f"notification:{notification.id}:push:{subscription.id}",
                    workspace_id=workspace_id,
                    notification_id=notification.id,
                )
        return notification

    def fanout_run(self, run: SyncRun) -> int:
        if run.status not in self.TERMINAL:
            return 0
        profile = self.db.get(SyncProfile, run.profile_id)
        if profile is None:
            return 0
        labels = {
            SyncRunStatus.SUCCEEDED: (
                NotificationSeverity.INFO,
                "Sync erfolgreich",
                "Der Sync wurde erfolgreich abgeschlossen.",
            ),
            SyncRunStatus.PARTIAL: (
                NotificationSeverity.WARNING,
                "Sync teilweise erfolgreich",
                "Einige Ereignisse oder Aktionen benötigen Aufmerksamkeit.",
            ),
            SyncRunStatus.FAILED: (
                NotificationSeverity.ERROR,
                "Sync fehlgeschlagen",
                "Der Sync konnte nicht erfolgreich abgeschlossen werden.",
            ),
            SyncRunStatus.CANCELED: (
                NotificationSeverity.WARNING,
                "Sync abgebrochen",
                "Der Sync wurde abgebrochen.",
            ),
            SyncRunStatus.SKIPPED: (
                NotificationSeverity.INFO,
                "Sync übersprungen",
                "Der Sync wurde ohne Änderungen übersprungen.",
            ),
        }
        severity, title, body = labels[run.status]
        members = self.db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.workspace_id == run.workspace_id,
                User.is_active.is_(True),
            )
        ).all()
        action_rows = self.db.scalars(
            select(SyncAction)
            .where(SyncAction.run_id == run.id)
            .order_by(SyncAction.ordinal, SyncAction.id)
        ).all()
        run_result = build_run_result(run, action_rows)
        song_events = [event for event in run_result.events if event.new_songs]
        can_group_songs = bool(song_events) and all(
            event.source_event_name is not None for event in song_events
        )
        new_song_count = sum(
            1
            for action in action_rows
            if action.kind == SyncActionKind.CREATE_SONG
            and action.status == SyncActionStatus.VERIFIED
        )
        created = 0
        for user, _membership in members:
            preference = self.preference(run.workspace_id, user.id)
            if run.status == SyncRunStatus.SUCCEEDED:
                notify_run = bool(
                    preference is not None and preference.success_notifications
                )
            elif run.status in {
                SyncRunStatus.PARTIAL,
                SyncRunStatus.FAILED,
                SyncRunStatus.CANCELED,
            }:
                notify_run = (
                    preference.failure_notifications
                    if preference is not None
                    else True
                )
            else:
                notify_run = False
            if notify_run:
                key = f"run:{run.id}:{run.status.value}:user:{user.id}"
                before = self.db.scalar(
                    select(Notification.id).where(Notification.deduplication_key == key)
                )
                self.create_for_user(
                    workspace_id=run.workspace_id,
                    user=user,
                    severity=severity,
                    category="sync_run",
                    title=title,
                    body=body,
                    run_id=run.id,
                    data={
                        "profile_id": str(run.profile_id),
                        "status": run.status.value,
                    },
                    deduplication_key=key,
                )
                if before is None:
                    created += 1
            notify_new_songs = (
                preference.new_song_notifications if preference is not None else True
            )
            if new_song_count and notify_new_songs:
                if can_group_songs:
                    for event in song_events:
                        key = f"run:{run.id}:new-songs:event:{event.id}:user:{user.id}"
                        before = self.db.scalar(
                            select(Notification.id).where(Notification.deduplication_key == key)
                        )
                        count = len(event.new_songs)
                        event_name = event.target_event_name or event.source_event_name or event.target_event_id or event.source_event_id
                        body_text = (
                            f"Ein neuer Song wurde für {event_name} in ChurchTools angelegt."
                            if count == 1
                            else f"{count} neue Songs wurden für {event_name} in ChurchTools angelegt."
                        )
                        self.create_for_user(
                            workspace_id=run.workspace_id,
                            user=user,
                            severity=NotificationSeverity.INFO,
                            category="new_songs",
                            title=f"Neue Songs · {event_name}",
                            body=body_text,
                            run_id=run.id,
                            data={
                                "profile_id": str(run.profile_id),
                                "status": run.status.value,
                                "event_plan_id": event.id,
                                "target_event_id": event.target_event_id,
                                "event_starts_at": (
                                    event.target_event_starts_at.isoformat()
                                    if event.target_event_starts_at
                                    else None
                                ),
                                "songs_created": count,
                            },
                            deduplication_key=key,
                        )
                        if before is None:
                            created += 1
                else:
                    key = f"run:{run.id}:new-songs:user:{user.id}"
                    before = self.db.scalar(
                        select(Notification.id).where(Notification.deduplication_key == key)
                    )
                    body_text = (
                        "Ein neuer Song wurde in ChurchTools angelegt."
                        if new_song_count == 1
                        else f"{new_song_count} neue Songs wurden in ChurchTools angelegt."
                    )
                    self.create_for_user(
                        workspace_id=run.workspace_id,
                        user=user,
                        severity=NotificationSeverity.INFO,
                        category="new_songs",
                        title="Neue ChurchTools-Songs",
                        body=body_text,
                        run_id=run.id,
                        data={
                            "profile_id": str(run.profile_id),
                            "status": run.status.value,
                            "songs_created": new_song_count,
                        },
                        deduplication_key=key,
                    )
                    if before is None:
                        created += 1
        run.notifications_fanned_out_at = utcnow()
        return created

    def fanout_pending_runs(self, limit: int = 100) -> int:
        runs = self.db.scalars(
            select(SyncRun)
            .where(
                SyncRun.status.in_(self.TERMINAL),
                SyncRun.notifications_fanned_out_at.is_(None),
            )
            .order_by(SyncRun.finished_at, SyncRun.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        total = sum(self.fanout_run(run) for run in runs)
        self.db.commit()
        return total


@dataclass(frozen=True)
class BatchResult:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0


@dataclass(frozen=True)
class DeliveryEnvelope:
    channel: str
    recipient: str
    payload: Mapping[str, Any]
    push_subscription_id: uuid.UUID | None = None
    push_subscription: Mapping[str, Any] | None = None


class OutboxConsumer:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        email_sender: EmailSender | None = None,
        push_sender: PushSender | None = None,
    ):
        self.database = database
        self.settings = settings
        self.email_sender = email_sender or SmtpEmailSender(settings)
        self.push_sender = push_sender or VapidPushSender(settings)

    def process_batch(
        self, *, now: datetime | None = None, limit: int | None = None
    ) -> BatchResult:
        requested_now = now
        batch_limit = max(1, min(limit or self.settings.outbox_batch_size, 500))
        claimed = 0
        delivered = retried = dead = 0
        for _ in range(batch_limit):
            claim_now = requested_now or utcnow()
            claim = self._claim_one(claim_now)
            if claim is None:
                break
            item_id, claim_token = claim
            claimed += 1
            outcome = self._deliver(item_id, claim_token, requested_now or utcnow())
            if outcome == OutboxStatus.DELIVERED:
                delivered += 1
            elif outcome == OutboxStatus.DEAD:
                dead += 1
            else:
                retried += 1
        return BatchResult(claimed, delivered, retried, dead)

    def _claim_one(self, now: datetime) -> tuple[uuid.UUID, str] | None:
        with self.database.session_factory() as db:
            set_worker_context(db)
            due = or_(
                and_(
                    NotificationOutbox.status.in_(
                        (OutboxStatus.PENDING, OutboxStatus.FAILED)
                    ),
                    NotificationOutbox.next_attempt_at <= now,
                ),
                and_(
                    NotificationOutbox.status == OutboxStatus.PROCESSING,
                    NotificationOutbox.next_attempt_at <= now,
                ),
            )
            item = db.scalar(
                select(NotificationOutbox)
                .where(due)
                .order_by(
                    NotificationOutbox.next_attempt_at,
                    NotificationOutbox.created_at,
                    NotificationOutbox.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if item is None:
                return None
            claim_token = uuid.uuid4().hex
            item.status = OutboxStatus.PROCESSING
            item.attempts += 1
            item.claim_token = claim_token
            item.next_attempt_at = now + timedelta(
                seconds=self.settings.outbox_lease_seconds
            )
            db.commit()
            return item.id, claim_token

    def _deliver(
        self, item_id: uuid.UUID, claim_token: str, now: datetime
    ) -> OutboxStatus:
        try:
            envelope = self._load_delivery(item_id, claim_token)
            if envelope is None:
                return OutboxStatus.FAILED
            with self._claim_heartbeat(item_id, claim_token) as claim_lost:
                if envelope.channel == "email":
                    self.email_sender.send(envelope.recipient, envelope.payload)
                elif envelope.channel == "push":
                    assert envelope.push_subscription is not None
                    self.push_sender.send(
                        envelope.push_subscription, envelope.payload
                    )
                    assert envelope.push_subscription_id is not None
                    self._touch_push_subscription(envelope.push_subscription_id)
                else:
                    raise DeliveryError("unknown_delivery_channel", permanent=True)
            # Even when a heartbeat failed, the final token-checked UPDATE is
            # authoritative.  Returning early here used to leave an item in
            # PROCESSING after the upstream side effect had succeeded, which
            # guaranteed a duplicate delivery after the lease expired.  If a
            # newer worker really took over, the CAS below still updates zero
            # rows and the stale worker cannot acknowledge its delivery.
            if claim_lost.is_set():
                logger.warning(
                    "outbox claim heartbeat was lost; attempting fenced completion",
                    extra={"outbox_id": str(item_id)},
                )
        except DeliveryError as exc:
            if (
                exc.code == "push_subscription_gone"
                and "envelope" in locals()
                and envelope is not None
                and envelope.push_subscription_id is not None
            ):
                self._revoke_push_subscription(envelope.push_subscription_id)
            return self._fail(
                item_id, claim_token, now, exc.code, exc.permanent
            )
        except Exception:
            # Deliberately discard exception strings: upstream libraries can
            # include endpoint URLs, recipient addresses or SMTP responses.
            return self._fail(
                item_id,
                claim_token,
                now,
                "unexpected_delivery_error",
                False,
            )
        with self.database.session_factory() as db:
            set_worker_context(db)
            result = db.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == item_id,
                    NotificationOutbox.status == OutboxStatus.PROCESSING,
                    NotificationOutbox.claim_token == claim_token,
                )
                .values(
                    status=OutboxStatus.DELIVERED,
                    delivered_at=now,
                    last_error_code=None,
                    claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()
            return (
                OutboxStatus.DELIVERED
                if result.rowcount == 1
                else OutboxStatus.FAILED
            )

    def _load_delivery(
        self, item_id: uuid.UUID, claim_token: str
    ) -> DeliveryEnvelope | None:
        with self.database.session_factory() as db:
            set_worker_context(db)
            item = db.scalar(
                select(NotificationOutbox).where(
                    NotificationOutbox.id == item_id,
                    NotificationOutbox.status == OutboxStatus.PROCESSING,
                    NotificationOutbox.claim_token == claim_token,
                )
            )
            if item is None:
                return None
            payload = SecretCipher(self.settings).decrypt_json(
                item.payload_encrypted, context=f"outbox:{item.id}"
            )
            if item.channel != "push":
                return DeliveryEnvelope(item.channel, item.recipient, payload)
            try:
                subscription_id = uuid.UUID(item.recipient)
            except ValueError as exc:
                raise DeliveryError(
                    "invalid_push_recipient", permanent=True
                ) from exc
            subscription = db.get(PushSubscription, subscription_id)
            if subscription is None or subscription.revoked_at is not None:
                raise DeliveryError("push_subscription_gone", permanent=True)
            decoded = SecretCipher(self.settings).decrypt_json(
                subscription.subscription_encrypted,
                context=f"push-subscription:{subscription.id}",
            )
            return DeliveryEnvelope(
                item.channel,
                item.recipient,
                payload,
                push_subscription_id=subscription.id,
                push_subscription=decoded,
            )

    def _touch_push_subscription(self, subscription_id: uuid.UUID) -> None:
        with self.database.session_factory() as db:
            set_worker_context(db)
            db.execute(
                update(PushSubscription)
                .where(
                    PushSubscription.id == subscription_id,
                    PushSubscription.revoked_at.is_(None),
                )
                .values(last_used_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            db.commit()

    def _revoke_push_subscription(self, subscription_id: uuid.UUID) -> None:
        with self.database.session_factory() as db:
            set_worker_context(db)
            db.execute(
                update(PushSubscription)
                .where(PushSubscription.id == subscription_id)
                .values(revoked_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            db.commit()

    @contextmanager
    def _claim_heartbeat(self, item_id: uuid.UUID, claim_token: str):
        stop = threading.Event()
        claim_lost = threading.Event()
        interval = max(5.0, self.settings.outbox_lease_seconds / 3)

        def renew() -> None:
            while not stop.wait(interval):
                try:
                    if not self._renew_claim(item_id, claim_token):
                        claim_lost.set()
                        return
                except Exception as exc:
                    claim_lost.set()
                    logger.error(
                        "outbox claim heartbeat failed",
                        extra={
                            "outbox_id": str(item_id),
                            "error_type": type(exc).__name__,
                        },
                    )
                    return

        heartbeat = threading.Thread(
            target=renew,
            name=f"outbox-heartbeat-{item_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            yield claim_lost
        finally:
            stop.set()
            heartbeat.join(timeout=5)

    def _renew_claim(
        self,
        item_id: uuid.UUID,
        claim_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Extend only the lease still owned by ``claim_token``."""

        renewal_time = now or utcnow()
        with self.database.session_factory() as db:
            set_worker_context(db)
            result = db.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == item_id,
                    NotificationOutbox.status == OutboxStatus.PROCESSING,
                    NotificationOutbox.claim_token == claim_token,
                )
                .values(
                    next_attempt_at=renewal_time
                    + timedelta(seconds=self.settings.outbox_lease_seconds)
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()
            return result.rowcount == 1

    def _fail(
        self,
        item_id: uuid.UUID,
        claim_token: str,
        now: datetime,
        code: str,
        permanent: bool,
    ) -> OutboxStatus:
        with self.database.session_factory() as db:
            set_worker_context(db)
            item = db.scalar(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.id == item_id,
                    NotificationOutbox.status == OutboxStatus.PROCESSING,
                    NotificationOutbox.claim_token == claim_token,
                )
                .with_for_update()
            )
            if item is None:
                return OutboxStatus.FAILED
            is_dead = permanent or item.attempts >= self.settings.outbox_max_attempts
            item.status = OutboxStatus.DEAD if is_dead else OutboxStatus.FAILED
            item.last_error_code = code[:100]
            item.claim_token = None
            if not is_dead:
                base = min(6 * 60 * 60, 15 * (2 ** max(0, item.attempts - 1)))
                jitter = int(item.id.int % max(1, base // 4 + 1))
                item.next_attempt_at = now + timedelta(seconds=base + jitter)
            db.commit()
            return item.status


class RetentionService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def cleanup(self, *, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        cutoff = now - timedelta(days=self.settings.retention_days)
        counts: dict[str, int] = {}
        with self.database.session_factory() as db:
            set_worker_context(db)
            terminal_run_ids = select(SyncRun.id).where(
                SyncRun.status.not_in((SyncRunStatus.QUEUED, SyncRunStatus.RUNNING)),
                SyncRun.created_at < cutoff,
            )
            statements = (
                (
                    "outbox",
                    delete(NotificationOutbox).where(
                        NotificationOutbox.status.in_(
                            (OutboxStatus.DELIVERED, OutboxStatus.DEAD)
                        ),
                        NotificationOutbox.created_at < cutoff,
                    ),
                ),
                ("notifications", delete(Notification).where(Notification.created_at < cutoff)),
                ("audit_events", delete(AuditEvent).where(AuditEvent.created_at < cutoff)),
                ("sync_actions", delete(SyncAction).where(SyncAction.run_id.in_(terminal_run_ids))),
                ("sync_runs", delete(SyncRun).where(SyncRun.id.in_(terminal_run_ids))),
                ("one_time_tokens", delete(OneTimeToken).where(OneTimeToken.expires_at < cutoff)),
                (
                    "auth_sessions",
                    delete(AuthSession).where(
                        or_(
                            AuthSession.expires_at < cutoff,
                            and_(
                                AuthSession.revoked_at.is_not(None),
                                AuthSession.revoked_at < cutoff,
                            ),
                        )
                    ),
                ),
                (
                    "invitations",
                    delete(WorkspaceInvitation).where(
                        or_(
                            WorkspaceInvitation.expires_at < cutoff,
                            and_(
                                WorkspaceInvitation.accepted_at.is_not(None),
                                WorkspaceInvitation.accepted_at < cutoff,
                            ),
                        )
                    ),
                ),
                (
                    "push_subscriptions",
                    delete(PushSubscription).where(
                        PushSubscription.revoked_at.is_not(None),
                        PushSubscription.revoked_at < cutoff,
                    ),
                ),
            )
            for name, statement in statements:
                result = db.execute(statement.execution_options(synchronize_session=False))
                counts[name] = max(0, result.rowcount or 0)
            db.commit()
        return counts
