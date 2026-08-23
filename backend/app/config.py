from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)


_MAX_SECRET_FILE_BYTES = 64 * 1024
_FILE_BACKED_FIELDS = {
    "database_url": "database_url_file",
    "database_admin_url": "database_admin_url_file",
    "database_owner_url": "database_owner_url_file",
    "redis_url": "redis_url_file",
    "application_secret": "application_secret_file",
    "encryption_secret": "encryption_secret_file",
    "encryption_previous_secrets": "encryption_previous_secrets_file",
    "smtp_password": "smtp_password_file",
    "vapid_private_key": "vapid_private_key_file",
    "telegram_bot_token": "telegram_bot_token_file",
    "bootstrap_admin_password": "bootstrap_admin_password_file",
}


def _setting_env_name(field_name: str) -> str:
    return f"WT_SYNC_{field_name.upper()}"


def _read_file_setting(path_value: object, file_field: str) -> str:
    env_name = _setting_env_name(file_field)
    try:
        path = os.fspath(path_value)
        if not path or "\x00" in path:
            raise OSError
        with Path(path).open("rb") as handle:
            payload = handle.read(_MAX_SECRET_FILE_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise SettingsError(
            f"{env_name} could not be read from its configured file"
        ) from None
    if len(payload) > _MAX_SECRET_FILE_BYTES:
        raise SettingsError(f"{env_name} exceeds the maximum supported size")
    if payload.endswith(b"\r\n"):
        payload = payload[:-2]
    elif payload.endswith(b"\n"):
        payload = payload[:-1]
    if b"\x00" in payload:
        raise SettingsError(f"{env_name} contains unsupported binary data")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise SettingsError(f"{env_name} must contain valid UTF-8 text") from None


class _FileBackedSettingsSource(PydanticBaseSettingsSource):
    """Resolve ``*_FILE`` values before model validation can expose inputs."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        sources: tuple[PydanticBaseSettingsSource, ...],
    ) -> None:
        super().__init__(settings_cls)
        self.sources = sources

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        source_values = tuple(source() for source in self.sources)
        loaded: dict[str, Any] = {}
        for target_field, file_field in _FILE_BACKED_FIELDS.items():
            direct_present = any(
                target_field in values for values in source_values
            )
            file_candidates = [
                values[file_field]
                for values in source_values
                if file_field in values
            ]
            if direct_present and file_candidates:
                raise SettingsError(
                    f"{_setting_env_name(target_field)} and "
                    f"{_setting_env_name(file_field)} must not both be set"
                )
            if not file_candidates:
                continue
            value: Any = _read_file_setting(file_candidates[0], file_field)
            if target_field == "encryption_previous_secrets":
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    raise SettingsError(
                        "WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS_FILE must contain a JSON object"
                    ) from None
                if not isinstance(value, dict):
                    raise SettingsError(
                        "WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS_FILE must contain a JSON object"
                    )
            loaded[target_field] = value
        return loaded


class Settings(BaseSettings):
    """Runtime settings.

    The development defaults make a local SQLite start possible. Production
    refuses the development secret and expects an explicit encryption secret.
    """

    model_config = SettingsConfigDict(
        env_prefix="WT_SYNC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    file_backed_fields: ClassVar[dict[str, str]] = _FILE_BACKED_FIELDS

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = Field(default="sqlite:///./worship-sync.db", repr=False)
    database_url_file: Path | None = Field(default=None, exclude=True, repr=False)
    database_admin_url: str | None = Field(default=None, exclude=True, repr=False)
    database_admin_url_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    database_owner_url: str | None = Field(default=None, exclude=True, repr=False)
    database_owner_url_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    api_prefix: str = "/api/v1"
    public_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = Field(default_factory=list, max_length=20)

    application_secret: SecretStr = SecretStr(
        "development-only-change-me-please-32-bytes"
    )
    application_secret_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    encryption_secret: SecretStr | None = None
    encryption_secret_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    encryption_key_version: int = Field(default=1, ge=1)
    # JSON object, for example {"1":"old secret"}. Old material is read-only;
    # every new ciphertext is written with ``encryption_key_version``.
    encryption_previous_secrets: dict[int, SecretStr] = Field(default_factory=dict)
    encryption_previous_secrets_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )

    registration_enabled: bool = True
    require_email_verification: bool = True
    expose_development_tokens: bool = False
    auto_create_schema: bool = False

    session_cookie_name: str = "wt_session"
    csrf_cookie_name: str = "wt_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    cookie_secure: bool = True
    session_ttl_seconds: int = 60 * 60 * 24 * 30
    verification_ttl_seconds: int = 60 * 60 * 24
    verification_resend_cooldown_seconds: int = Field(default=300, ge=30, le=3600)
    recovery_ttl_seconds: int = 60 * 30
    admin_mfa_max_age_seconds: int = Field(default=60 * 60 * 12, ge=300, le=604800)
    manual_run_cooldown_seconds: int = 60 * 30
    workspace_quota_per_user: int = 1
    invite_resend_cooldown_seconds: int = Field(default=300, ge=30, le=3600)
    max_connections_per_workspace: int = Field(default=20, ge=2, le=1000)
    max_push_subscriptions_per_user_workspace: int = Field(default=10, ge=1, le=100)
    password_hash_max_concurrency: int = Field(default=2, ge=1, le=16)
    password_hash_acquire_timeout_seconds: float = Field(
        default=0.25, ge=0.05, le=5.0
    )

    redis_url: str = Field(default="redis://localhost:6379/0", repr=False)
    redis_url_file: Path | None = Field(default=None, exclude=True, repr=False)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_password_file: Path | None = Field(default=None, exclude=True, repr=False)
    smtp_from: str = "Worship Sync <noreply@localhost>"
    smtp_starttls: bool = True
    smtp_implicit_tls: bool = False
    smtp_timeout_seconds: float = Field(default=15.0, ge=1.0, le=30.0)
    vapid_public_key: str | None = None
    vapid_private_key: SecretStr | None = None
    vapid_private_key_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    vapid_subject: str | None = None
    web_push_allowed_host_suffixes: list[str] = Field(
        default_factory=lambda: [
            "fcm.googleapis.com",
            ".push.services.mozilla.com",
            "web.push.apple.com",
        ]
    )
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_bot_token_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )
    telegram_chat_id: str | None = None
    telegram_workspace_id: uuid.UUID | None = None

    outbox_batch_size: int = Field(default=10, ge=1, le=50)
    outbox_max_attempts: int = Field(default=8, ge=1, le=25)
    outbox_lease_seconds: int = Field(default=300, ge=120, le=3600)
    retention_days: int = Field(default=90, ge=1, le=3650)
    scheduler_redelivery_seconds: int = 300

    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None
    bootstrap_admin_password_file: Path | None = Field(
        default=None, exclude=True, repr=False
    )

    default_page_size: int = 50
    max_page_size: int = 200

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        original_sources = (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
        return (
            _FileBackedSettingsSource(settings_cls, original_sources),
            *original_sources,
        )

    @field_validator("database_url", "database_admin_url", "database_owner_url")
    @classmethod
    def normalize_postgres_scheme(cls, value: str | None) -> str | None:
        # SQLAlchemy's psycopg 3 dialect is explicit; accepting the common
        # shorthand keeps container configuration unsurprising.
        if value is None:
            return None
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value

    @field_validator("api_prefix", mode="before")
    @classmethod
    def canonicalize_api_prefix(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("WT_SYNC_API_PREFIX must be an absolute URL path")
        if (
            not value
            or len(value) > 100
            or value != value.strip()
            or not value.startswith("/")
            or value.startswith("//")
            or "?" in value
            or "#" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("WT_SYNC_API_PREFIX must be a bounded absolute URL path")
        canonical = value.rstrip("/")
        decoded = unquote(canonical)
        if (
            not canonical
            or "//" in canonical
            or "//" in decoded
            or "?" in decoded
            or "#" in decoded
            or "\\" in decoded
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in decoded
            )
            or any(segment in {".", ".."} for segment in decoded.split("/"))
        ):
            raise ValueError(
                "WT_SYNC_API_PREFIX must not contain duplicate or traversal segments"
            )
        return canonical

    @field_validator("cors_origins")
    @classmethod
    def canonicalize_cors_origins(cls, values: list[str]) -> list[str]:
        canonical: list[str] = []
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or value.casefold() in {"null", "*"}
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("WT_SYNC_CORS_ORIGINS must contain explicit origins")
            parsed = urlsplit(value)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(
                    "WT_SYNC_CORS_ORIGINS contains an invalid port"
                ) from exc
            if (
                parsed.scheme.casefold() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "WT_SYNC_CORS_ORIGINS entries must be HTTP(S) origins without credentials or paths"
                )
            hostname = parsed.hostname.casefold().rstrip(".")
            host = f"[{hostname}]" if ":" in hostname else hostname
            default_port = 443 if parsed.scheme.casefold() == "https" else 80
            origin = f"{parsed.scheme.casefold()}://{host}"
            if port is not None and port != default_port:
                origin += f":{port}"
            if origin not in canonical:
                canonical.append(origin)
        return canonical

    @field_validator(
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "vapid_public_key",
        "vapid_private_key",
        "vapid_subject",
        "telegram_bot_token",
        "telegram_chat_id",
        "telegram_workspace_id",
        mode="before",
    )
    @classmethod
    def empty_optional_value_is_none(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            return None if not value.get_secret_value().strip() else value
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def model_post_init(self, __context: object) -> None:
        if self.smtp_starttls and self.smtp_implicit_tls:
            raise ValueError(
                "WT_SYNC_SMTP_STARTTLS and WT_SYNC_SMTP_IMPLICIT_TLS "
                "must not both be true"
            )
        minimum_outbox_lease = int(self.smtp_timeout_seconds * 6) + 30
        if self.outbox_lease_seconds < minimum_outbox_lease:
            raise ValueError(
                "WT_SYNC_OUTBOX_LEASE_SECONDS must exceed the bounded delivery timeout"
            )
        if self.environment == "production":
            if self.expose_development_tokens:
                raise ValueError(
                    "WT_SYNC_EXPOSE_DEVELOPMENT_TOKENS must be false in production"
                )
            if not self.api_prefix.startswith("/api/"):
                raise ValueError(
                    "WT_SYNC_API_PREFIX must stay below /api/ in production for gateway compatibility"
                )
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError(
                    "WT_SYNC_CORS_ORIGINS must contain HTTPS origins in production"
                )
            if self.encryption_secret is None:
                raise ValueError("WT_SYNC_ENCRYPTION_SECRET must be set in production")
            if self.require_email_verification and not self.smtp_host:
                raise ValueError(
                    "WT_SYNC_SMTP_HOST must be set in production when email verification is required"
                )
            application_secret = self.application_secret.get_secret_value()
            encryption_secret = self.encryption_secret.get_secret_value()
            for setting_name, secret in (
                ("WT_SYNC_APPLICATION_SECRET", application_secret),
                ("WT_SYNC_ENCRYPTION_SECRET", encryption_secret),
            ):
                lowered = secret.casefold()
                if len(secret) < 32 or any(
                    marker in lowered
                    for marker in (
                        "development-only",
                        "replace-",
                        "replace_",
                        "change-me",
                        "changeme",
                    )
                ):
                    raise ValueError(
                        f"{setting_name} must be a non-placeholder secret of at least 32 characters"
                    )
            for version, previous in self.encryption_previous_secrets.items():
                previous_value = previous.get_secret_value()
                lowered = previous_value.casefold()
                if version < 1 or len(previous_value) < 32 or any(
                    marker in lowered
                    for marker in (
                        "development-only",
                        "replace-",
                        "replace_",
                        "change-me",
                        "changeme",
                    )
                ):
                    raise ValueError(
                        "WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS contains an invalid key"
                    )
            if self.encryption_key_version in self.encryption_previous_secrets:
                raise ValueError(
                    "The active encryption key version must not also be in the previous-key map"
                )
            if hmac.compare_digest(application_secret, encryption_secret):
                raise ValueError(
                    "WT_SYNC_APPLICATION_SECRET and WT_SYNC_ENCRYPTION_SECRET must differ"
                )
            if not self.cookie_secure:
                raise ValueError("WT_SYNC_COOKIE_SECURE must be true in production")
            database_url = urlsplit(self.database_url)
            if (
                database_url.scheme != "postgresql+psycopg"
                or not database_url.hostname
                or database_url.path in ("", "/")
            ):
                raise ValueError(
                    "WT_SYNC_DATABASE_URL must use a PostgreSQL psycopg DSN with host and database in production"
                )
            if self.database_admin_url is not None:
                admin_url = urlsplit(self.database_admin_url)
                if (
                    admin_url.scheme != "postgresql+psycopg"
                    or not admin_url.hostname
                    or admin_url.path in ("", "/")
                ):
                    raise ValueError(
                        "WT_SYNC_DATABASE_ADMIN_URL must use a PostgreSQL psycopg DSN with host and database in production"
                    )
                database_username = unquote(database_url.username or "")
                admin_username = unquote(admin_url.username or "")
                if database_username != "worshipsync_api":
                    raise ValueError(
                        "WT_SYNC_DATABASE_URL must use the worshipsync_api role when WT_SYNC_DATABASE_ADMIN_URL is configured"
                    )
                if admin_username != "worshipsync_admin":
                    raise ValueError(
                        "WT_SYNC_DATABASE_ADMIN_URL must use the worshipsync_admin role"
                    )
                if (
                    admin_url.hostname != database_url.hostname
                    or (admin_url.port or 5432) != (database_url.port or 5432)
                    or admin_url.path != database_url.path
                ):
                    raise ValueError(
                        "WT_SYNC_DATABASE_ADMIN_URL must target the same PostgreSQL database as WT_SYNC_DATABASE_URL"
                    )
            parsed = urlsplit(self.public_base_url)
            try:
                parsed.port
            except ValueError as exc:
                raise ValueError(
                    "WT_SYNC_PUBLIC_BASE_URL must be a valid HTTPS origin"
                ) from exc
            if (
                parsed.scheme.casefold() != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or bool(parsed.query)
                or bool(parsed.fragment)
            ):
                raise ValueError(
                    "WT_SYNC_PUBLIC_BASE_URL must be an HTTPS origin without path, query, or credentials"
                )

    @property
    def encryption_key(self) -> bytes:
        material = (
            self.encryption_secret.get_secret_value()
            if self.encryption_secret is not None
            else self.application_secret.get_secret_value()
        )
        return hashlib.sha256(material.encode("utf-8")).digest()

    @property
    def encryption_keys(self) -> dict[int, bytes]:
        keys = {
            version: hashlib.sha256(secret.get_secret_value().encode("utf-8")).digest()
            for version, secret in self.encryption_previous_secrets.items()
        }
        keys[self.encryption_key_version] = self.encryption_key
        return keys


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
