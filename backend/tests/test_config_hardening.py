from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


def _production_settings(**updates) -> Settings:
    values = {
        "environment": "production",
        "database_url": "postgresql://worshipsync_api:secret@postgres/worshipsync",
        "public_base_url": "https://sync.example.org",
        "application_secret": "application-secret-production-value-1234567890",
        "encryption_secret": "encryption-secret-production-value-0987654321",
        "cookie_secure": True,
        "smtp_host": "smtp.example.org",
    }
    values.update(updates)
    return Settings(**values)


def test_valid_production_settings_are_normalized():
    settings = _production_settings(
        database_admin_url="postgresql://worshipsync_admin:secret@postgres/worshipsync"
    )

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.database_admin_url.startswith("postgresql+psycopg://")
    assert settings.cookie_secure


def test_api_prefix_is_canonical_and_bounded():
    assert Settings(api_prefix="/api/custom/").api_prefix == "/api/custom"

    for value in (
        "api/v1",
        "//api/v1",
        "/api//v1",
        "/api/../v1",
        "/api/%2e%2e/v1",
        "/api/%2Fv1",
        "/api/v1?debug=1",
        "/api/v1#fragment",
        "/api/v1\\other",
        "/" + "a" * 101,
    ):
        with pytest.raises(ValidationError):
            Settings(api_prefix=value)

    with pytest.raises(ValidationError, match="below /api/"):
        _production_settings(api_prefix="/internal/v1")


def test_cors_origins_are_canonical_deduplicated_and_production_https_only():
    settings = Settings(
        cors_origins=[
            "https://APP.EXAMPLE.ORG:443/",
            "https://app.example.org",
            "http://localhost:5173",
        ]
    )
    assert settings.cors_origins == [
        "https://app.example.org",
        "http://localhost:5173",
    ]

    production = _production_settings(
        cors_origins=["https://app.example.org", "https://admin.example.org:8443"]
    )
    assert production.cors_origins == [
        "https://app.example.org",
        "https://admin.example.org:8443",
    ]

    with pytest.raises(ValidationError, match="HTTPS origins"):
        _production_settings(cors_origins=["http://app.example.org"])


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        " https://app.example.org",
        "https://user:password@app.example.org",
        "https://app.example.org/path",
        "https://app.example.org?query=1",
        "https://app.example.org#fragment",
    ],
)
def test_cors_rejects_credentialed_or_non_origin_values(origin):
    with pytest.raises(ValidationError):
        Settings(cors_origins=[origin])


def test_new_security_and_resource_settings_are_bounded():
    assert Settings().admin_mfa_max_age_seconds == 12 * 60 * 60
    for field, value in (
        ("admin_mfa_max_age_seconds", 299),
        ("verification_resend_cooldown_seconds", 29),
        ("invite_resend_cooldown_seconds", 29),
        ("max_connections_per_workspace", 1),
        ("max_push_subscriptions_per_user_workspace", 0),
        ("password_hash_max_concurrency", 0),
        ("password_hash_acquire_timeout_seconds", 0.01),
    ):
        with pytest.raises(ValidationError):
            Settings(**{field: value})


def test_production_admin_database_role_must_be_distinct_from_api_role():
    with pytest.raises(ValidationError, match="worshipsync_admin"):
        _production_settings(
            database_admin_url="postgresql://worshipsync_api:other-secret@postgres/worshipsync"
        )


def test_production_api_requires_restricted_admin_database_url():
    with pytest.raises(RuntimeError, match="WT_SYNC_DATABASE_ADMIN_URL"):
        create_app(_production_settings(), database=object())


def test_production_email_verification_requires_smtp():
    with pytest.raises(ValidationError, match="WT_SYNC_SMTP_HOST"):
        _production_settings(smtp_host=None)


def test_smtp_tls_modes_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="must not both be true"):
        Settings(smtp_starttls=True, smtp_implicit_tls=True)

    implicit_tls = Settings(smtp_starttls=False, smtp_implicit_tls=True)
    assert implicit_tls.smtp_implicit_tls


@pytest.mark.parametrize(
    "updates",
    [
        {"public_base_url": "http://sync.example.org"},
        {"public_base_url": "https://sync.example.org/path"},
        {"cookie_secure": False},
        {"expose_development_tokens": True},
        {"database_url": "sqlite:///production.db"},
        {"application_secret": "too-short"},
        {"encryption_secret": "too-short"},
        {
            "application_secret": "same-secret-value-with-at-least-32-characters",
            "encryption_secret": "same-secret-value-with-at-least-32-characters",
        },
        {
            "application_secret": "replace-with-a-real-secret-value-123456789",
        },
    ],
)
def test_unsafe_production_settings_fail_fast(updates):
    with pytest.raises(ValidationError):
        _production_settings(**updates)


def test_optional_delivery_settings_normalize_blank_env_values():
    settings = Settings(
        smtp_host="  ",
        smtp_username="",
        smtp_password=" ",
        vapid_public_key="",
        vapid_private_key=" ",
        vapid_subject="",
    )

    assert settings.smtp_host is None
    assert settings.smtp_username is None
    assert settings.smtp_password is None
    assert settings.vapid_public_key is None
    assert settings.vapid_private_key is None
    assert settings.vapid_subject is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("outbox_batch_size", 0),
        ("outbox_batch_size", 501),
        ("outbox_max_attempts", 0),
        ("outbox_lease_seconds", 14),
        ("retention_days", 0),
        ("retention_days", 3651),
    ],
)
def test_retention_and_outbox_settings_are_bounded(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})
