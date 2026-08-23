from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Keep packaged ``app`` imports working when pytest is launched from the
# repository root (as CI does).
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.database import Database


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite://",
        application_secret="test-application-secret-with-at-least-32-bytes",
        encryption_secret="test-encryption-secret-with-at-least-32-bytes",
        cookie_secure=False,
        require_email_verification=False,
        auto_create_schema=False,
        workspace_quota_per_user=3,
    )


@pytest.fixture()
def database(settings: Settings):
    database = Database(settings)
    database.create_schema()
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture()
def db(database: Database):
    session = database.session_factory()
    try:
        yield session
    finally:
        session.close()
