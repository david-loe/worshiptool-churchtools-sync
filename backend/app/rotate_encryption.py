"""Re-encrypt stored secrets with the configured active key version.

Stop API, scheduler, and workers before running this command. The old key must
remain in ``WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS`` until this transaction and a
backup have both been verified.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy import select

from .config import Settings
from .database import Database
from .models import (
    NotificationOutbox,
    ProviderConnection,
    PushSubscription,
    User,
)
from .outbox import set_worker_context
from .security import SecretCipher


def _version(ciphertext: str) -> int:
    label, _separator, _payload = ciphertext.partition(".")
    if not label.startswith("v") or not label[1:].isdigit():
        raise ValueError("encrypted value has an invalid version prefix")
    return int(label[1:])


def _rotate_text(
    cipher: SecretCipher,
    value: str | None,
    *,
    context: str,
    current_version: int,
) -> tuple[str | None, bool]:
    if value is None or _version(value) == current_version:
        return value, False
    cleartext = cipher.decrypt_text(value, context=context)
    return cipher.encrypt_text(cleartext, context=context), True


def _locked(session, model) -> Iterable:
    return session.scalars(select(model).with_for_update()).all()


def rotate(settings: Settings | None = None) -> dict[str, int]:
    settings = settings or Settings()
    if not settings.encryption_previous_secrets:
        raise RuntimeError(
            "WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS must contain the old key"
        )
    database = Database(settings)
    cipher = SecretCipher(settings)
    counts = {
        "provider_connections": 0,
        "totp_secrets": 0,
        "push_subscriptions": 0,
        "outbox_payloads": 0,
    }
    try:
        with database.session_factory() as db:
            set_worker_context(db)
            for connection in _locked(db, ProviderConnection):
                rotated, changed = _rotate_text(
                    cipher,
                    connection.credentials_encrypted,
                    context=f"connection:{connection.id}",
                    current_version=settings.encryption_key_version,
                )
                if changed:
                    connection.credentials_encrypted = rotated
                    connection.encryption_key_version = settings.encryption_key_version
                    counts["provider_connections"] += 1
            for user in _locked(db, User):
                for attribute, suffix in (
                    ("totp_secret_encrypted", "totp"),
                    ("totp_pending_secret_encrypted", "totp-pending"),
                ):
                    rotated, changed = _rotate_text(
                        cipher,
                        getattr(user, attribute),
                        context=f"user:{user.id}:{suffix}",
                        current_version=settings.encryption_key_version,
                    )
                    if changed:
                        setattr(user, attribute, rotated)
                        counts["totp_secrets"] += 1
            for subscription in _locked(db, PushSubscription):
                rotated, changed = _rotate_text(
                    cipher,
                    subscription.subscription_encrypted,
                    context=f"push-subscription:{subscription.id}",
                    current_version=settings.encryption_key_version,
                )
                if changed:
                    subscription.subscription_encrypted = rotated
                    counts["push_subscriptions"] += 1
            for item in _locked(db, NotificationOutbox):
                rotated, changed = _rotate_text(
                    cipher,
                    item.payload_encrypted,
                    context=f"outbox:{item.id}",
                    current_version=settings.encryption_key_version,
                )
                if changed:
                    item.payload_encrypted = rotated
                    counts["outbox_payloads"] += 1
            db.commit()
    finally:
        database.dispose()
    return counts


def main() -> None:
    print(json.dumps(rotate(), sort_keys=True))


if __name__ == "__main__":
    main()
