from __future__ import annotations

from app.config import Settings
from app.security import (
    SecretCipher,
    generate_recovery_codes,
    hash_password,
    hash_recovery_codes,
    verify_and_consume_recovery_code,
    verify_password,
)


def test_secret_cipher_is_authenticated_and_context_bound(settings):
    cipher = SecretCipher(settings)
    credentials = {"email": "person@example.org", "password": "very-secret"}

    encrypted = cipher.encrypt_json(credentials, context="connection:one")

    assert "very-secret" not in encrypted
    assert cipher.decrypt_json(encrypted, context="connection:one") == credentials

    try:
        cipher.decrypt_json(encrypted, context="connection:two")
    except Exception:
        pass
    else:
        raise AssertionError("ciphertext must not decrypt in another tenant context")


def test_password_and_one_time_recovery_codes(settings):
    password_hash = hash_password("correct horse battery staple")
    assert verify_password(password_hash, "correct horse battery staple")
    assert not verify_password(password_hash, "wrong password")

    codes = generate_recovery_codes(2)
    hashes = hash_recovery_codes(settings, codes)
    accepted, remaining = verify_and_consume_recovery_code(settings, hashes, codes[0])
    assert accepted
    assert len(remaining) == 1
    accepted_again, _ = verify_and_consume_recovery_code(settings, remaining, codes[0])
    assert not accepted_again


def test_secret_cipher_reads_previous_key_version_during_rotation(settings):
    old_settings = Settings(
        environment="test",
        application_secret=settings.application_secret,
        encryption_secret="old-encryption-secret-with-at-least-32-bytes",
        encryption_key_version=1,
    )
    ciphertext = SecretCipher(old_settings).encrypt_text(
        "provider-secret", context="connection:one"
    )
    rotated_settings = Settings(
        environment="test",
        application_secret=settings.application_secret,
        encryption_secret="new-encryption-secret-with-at-least-32-bytes",
        encryption_key_version=2,
        encryption_previous_secrets={
            1: "old-encryption-secret-with-at-least-32-bytes"
        },
    )
    rotated_cipher = SecretCipher(rotated_settings)

    assert (
        rotated_cipher.decrypt_text(ciphertext, context="connection:one")
        == "provider-secret"
    )
    assert rotated_cipher.encrypt_text("new", context="connection:one").startswith(
        "v2."
    )
