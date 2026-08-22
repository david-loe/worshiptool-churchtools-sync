from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Settings


_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


class PasswordHashCapacityError(RuntimeError):
    """The bounded Argon2 pool is saturated; callers should retry later."""


class _PasswordWorkGate:
    """Process-wide admission control for memory-hard Argon2 operations.

    A condition rather than replaceable semaphores keeps one shared active
    count even when tests or multiple app instances provide different limits.
    Production requests all use the same immutable Settings instance.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0

    def acquire(self, maximum: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._active >= maximum:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._active += 1
            return True

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()


_PASSWORD_WORK_GATE = _PasswordWorkGate()


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def validate_password(value: str) -> str:
    if len(value) < 12:
        raise ValueError("Das Passwort muss mindestens 12 Zeichen lang sein.")
    if len(value.encode("utf-8")) > 1024:
        raise ValueError("Das Passwort ist zu lang.")
    return value


def hash_password(
    value: str,
    *,
    max_concurrency: int = 2,
    acquire_timeout: float = 0.25,
) -> str:
    validated = validate_password(value)
    if not _PASSWORD_WORK_GATE.acquire(max_concurrency, acquire_timeout):
        raise PasswordHashCapacityError("password hashing capacity is saturated")
    try:
        return _PASSWORD_HASHER.hash(validated)
    finally:
        _PASSWORD_WORK_GATE.release()


def verify_password(
    password_hash: str,
    candidate: str,
    *,
    max_concurrency: int = 2,
    acquire_timeout: float = 0.25,
) -> bool:
    if len(candidate.encode("utf-8")) > 1024:
        return False
    if not _PASSWORD_WORK_GATE.acquire(max_concurrency, acquire_timeout):
        raise PasswordHashCapacityError("password verification capacity is saturated")
    try:
        try:
            return _PASSWORD_HASHER.verify(password_hash, candidate)
        except (VerifyMismatchError, InvalidHashError):
            return False
    finally:
        _PASSWORD_WORK_GATE.release()


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def random_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def token_hash(settings: Settings, token: str) -> str:
    return hmac.new(
        settings.application_secret.get_secret_value().encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SessionTokens:
    session_token: str
    csrf_token: str
    expires_at: datetime


def issue_session_tokens(settings: Settings) -> SessionTokens:
    return SessionTokens(
        session_token=random_token(),
        csrf_token=random_token(),
        expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
    )


class SecretCipher:
    """Versioned authenticated encryption for provider and TOTP secrets."""

    def __init__(self, settings: Settings):
        self._keys = settings.encryption_keys
        self._key = self._keys[settings.encryption_key_version]
        self._version = settings.encryption_key_version

    def encrypt_text(self, value: str, *, context: str) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce, value.encode("utf-8"), context.encode("utf-8")
        )
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"v{self._version}.{payload}"

    def decrypt_text(self, value: str, *, context: str) -> str:
        version_label, encoded = value.split(".", 1)
        if not version_label.startswith("v") or not version_label[1:].isdigit():
            raise ValueError("unsupported encryption key version")
        key = self._keys.get(int(version_label[1:]))
        if key is None:
            raise ValueError("unsupported encryption key version")
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return AESGCM(key).decrypt(
            payload[:12], payload[12:], context.encode("utf-8")
        ).decode("utf-8")

    def encrypt_json(self, value: dict[str, Any], *, context: str) -> str:
        serialized = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return self.encrypt_text(serialized, context=context)

    def decrypt_json(self, value: str, *, context: str) -> dict[str, Any]:
        decoded = json.loads(self.decrypt_text(value, context=context))
        if not isinstance(decoded, dict):
            raise ValueError("encrypted secret payload must be an object")
        return decoded


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", code)) and pyotp.TOTP(secret).verify(
        code, valid_window=1
    )


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=email, issuer_name="WorshipTools ChurchTools Sync"
    )


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(count)]


def verify_and_consume_recovery_code(
    settings: Settings, hashes: list[str], candidate: str
) -> tuple[bool, list[str]]:
    candidate_hash = token_hash(settings, f"recovery:{candidate.strip().lower()}")
    for index, stored_hash in enumerate(hashes):
        if hmac.compare_digest(stored_hash, candidate_hash):
            return True, hashes[:index] + hashes[index + 1 :]
    return False, hashes


def hash_recovery_codes(settings: Settings, codes: list[str]) -> list[str]:
    return [token_hash(settings, f"recovery:{code.lower()}") for code in codes]
