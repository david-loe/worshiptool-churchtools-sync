from __future__ import annotations

import os

import pytest
from pydantic import SecretStr
from pydantic_settings import SettingsError

from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_wt_sync_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("WT_SYNC_"):
            monkeypatch.delenv(key)


def _plain(value):
    return value.get_secret_value() if isinstance(value, SecretStr) else value


@pytest.mark.parametrize(
    "environment_name,attribute,payload,expected",
    [
        (
            "WT_SYNC_DATABASE_URL_FILE",
            "database_url",
            b"postgres://api:password@postgres/worshipsync\n",
            "postgresql+psycopg://api:password@postgres/worshipsync",
        ),
        (
            "WT_SYNC_DATABASE_OWNER_URL_FILE",
            "database_owner_url",
            b"postgresql://owner:password@postgres/worshipsync\n",
            "postgresql+psycopg://owner:password@postgres/worshipsync",
        ),
        (
            "WT_SYNC_DATABASE_ADMIN_URL_FILE",
            "database_admin_url",
            b"postgresql://admin:password@postgres/worshipsync\n",
            "postgresql+psycopg://admin:password@postgres/worshipsync",
        ),
        (
            "WT_SYNC_REDIS_URL_FILE",
            "redis_url",
            b"redis://:password@redis:6379/0\n",
            "redis://:password@redis:6379/0",
        ),
        (
            "WT_SYNC_APPLICATION_SECRET_FILE",
            "application_secret",
            b"application-secret-from-file\n",
            "application-secret-from-file",
        ),
        (
            "WT_SYNC_ENCRYPTION_SECRET_FILE",
            "encryption_secret",
            b"encryption-secret-from-file\n",
            "encryption-secret-from-file",
        ),
        (
            "WT_SYNC_SMTP_PASSWORD_FILE",
            "smtp_password",
            b"smtp password with spaces\n",
            "smtp password with spaces",
        ),
        (
            "WT_SYNC_VAPID_PRIVATE_KEY_FILE",
            "vapid_private_key",
            b"first pem line\nsecond pem line\n",
            "first pem line\nsecond pem line",
        ),
        (
            "WT_SYNC_TELEGRAM_BOT_TOKEN_FILE",
            "telegram_bot_token",
            b"telegram-token\n",
            "telegram-token",
        ),
        (
            "WT_SYNC_BOOTSTRAP_ADMIN_PASSWORD_FILE",
            "bootstrap_admin_password",
            b"bootstrap password\n",
            "bootstrap password",
        ),
    ],
)
def test_supported_settings_load_from_files(
    tmp_path, monkeypatch, environment_name, attribute, payload, expected
):
    secret_file = tmp_path / environment_name.casefold()
    secret_file.write_bytes(payload)
    monkeypatch.setenv(environment_name, str(secret_file))

    settings = Settings(_env_file=None)

    assert _plain(getattr(settings, attribute)) == expected


def test_previous_encryption_keys_load_as_json_object(tmp_path, monkeypatch):
    secret_file = tmp_path / "previous-keys"
    secret_file.write_text('{"1":"old-secret-value"}\n', encoding="utf-8")
    monkeypatch.setenv(
        "WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS_FILE", str(secret_file)
    )

    settings = Settings(_env_file=None)

    assert settings.encryption_previous_secrets[1].get_secret_value() == (
        "old-secret-value"
    )


@pytest.mark.parametrize(
    "payload,expected",
    [
        (b"  keep surrounding spaces  \n", "  keep surrounding spaces  "),
        (b"windows newline\r\n", "windows newline"),
        (b"two newlines\n\n", "two newlines\n"),
        (b"carriage return only\r", "carriage return only\r"),
    ],
)
def test_file_loader_removes_only_one_terminal_newline(
    tmp_path, monkeypatch, payload, expected
):
    secret_file = tmp_path / "whitespace-secret"
    secret_file.write_bytes(payload)
    monkeypatch.setenv("WT_SYNC_APPLICATION_SECRET_FILE", str(secret_file))

    settings = Settings(_env_file=None)

    assert settings.application_secret.get_secret_value() == expected


@pytest.mark.parametrize(
    "target_field,file_field",
    list(Settings.file_backed_fields.items()),
)
def test_direct_and_file_values_conflict_without_leaking_inputs(
    tmp_path, monkeypatch, target_field, file_field
):
    direct_secret = (
        '{"1":"direct-value-must-not-leak"}'
        if target_field == "encryption_previous_secrets"
        else "direct-value-must-not-leak"
    )
    secret_file = tmp_path / "configured-secret-path-must-not-leak"
    secret_file.write_text("file-value-must-not-leak", encoding="utf-8")
    monkeypatch.setenv(f"WT_SYNC_{target_field.upper()}", direct_secret)
    monkeypatch.setenv(f"WT_SYNC_{file_field.upper()}", str(secret_file))

    with pytest.raises(SettingsError) as error:
        Settings(_env_file=None)

    message = str(error.value)
    assert "must not both be set" in message
    assert direct_secret not in message
    assert str(secret_file) not in message
    assert "file-value-must-not-leak" not in message


@pytest.mark.parametrize("failure", ["missing", "directory", "utf8", "nul", "large"])
def test_secret_file_read_errors_are_safe(tmp_path, monkeypatch, failure):
    secret_file = tmp_path / f"sensitive-{failure}-path"
    if failure == "directory":
        secret_file.mkdir()
    elif failure == "utf8":
        secret_file.write_bytes(b"\xff\xfe")
    elif failure == "nul":
        secret_file.write_bytes(b"secret\x00suffix")
    elif failure == "large":
        secret_file.write_bytes(b"x" * (64 * 1024 + 1))
    monkeypatch.setenv("WT_SYNC_APPLICATION_SECRET_FILE", str(secret_file))

    with pytest.raises(SettingsError) as error:
        Settings(_env_file=None)

    message = str(error.value)
    assert "WT_SYNC_APPLICATION_SECRET_FILE" in message
    assert str(secret_file) not in message


def test_dotenv_direct_and_file_values_also_conflict(tmp_path, monkeypatch):
    secret_file = tmp_path / "dotenv-secret-path"
    secret_file.write_text("file-secret", encoding="utf-8")
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "WT_SYNC_APPLICATION_SECRET=dotenv-direct-secret\n"
        f"WT_SYNC_APPLICATION_SECRET_FILE={secret_file}\n",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError) as error:
        Settings(_env_file=dotenv)

    message = str(error.value)
    assert "dotenv-direct-secret" not in message
    assert str(secret_file) not in message


def test_file_path_fields_are_not_serialized_or_represented(tmp_path, monkeypatch):
    secret_file = tmp_path / "hidden-path"
    secret_file.write_text("hidden-secret", encoding="utf-8")
    monkeypatch.setenv("WT_SYNC_APPLICATION_SECRET_FILE", str(secret_file))

    settings = Settings(_env_file=None)

    assert "application_secret_file" not in settings.model_dump()
    assert str(secret_file) not in repr(settings)
    assert "hidden-secret" not in repr(settings)
