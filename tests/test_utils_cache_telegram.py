from datetime import datetime, timedelta, timezone

import pytest

import telegram
from cache import Cacher, YamlDatabase
from utils import slice_list


def test_slice_list_supports_common_slice_forms():
    values = ["a", "b", "c"]

    assert slice_list(values, "[:]") == values
    assert slice_list(values, "[:-1]") == ["a", "b"]
    assert slice_list(values, "[-1]") == ["c"]


def test_slice_list_rejects_invalid_input():
    with pytest.raises(ValueError):
        slice_list(["a"], "[abc]")


def test_cache_cleaning_removes_past_entries(tmp_path):
    db_path = tmp_path / "db.yaml"
    db = YamlDatabase(str(db_path))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    db.insert(
        "cache",
        [
            {"event_datetime": past.isoformat(), "hash": "past", "last_sync": past.isoformat()},
            {"event_datetime": future.isoformat(), "hash": "future", "last_sync": future.isoformat()},
        ],
    )

    Cacher(db)

    assert db.get("cache") == [{"event_datetime": future.isoformat(), "hash": "future", "last_sync": future.isoformat()}]


def test_telegram_returns_when_credentials_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    called = False

    def post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(telegram.requests, "post", post)

    telegram.send_telegram_message("message")

    assert called is False


def test_telegram_uses_timeout(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))

        class Response:
            status_code = 200

        return Response()

    monkeypatch.setattr(telegram.requests, "post", post)

    telegram.send_telegram_message("message")

    assert calls[0][1]["timeout"] == telegram.REQUEST_TIMEOUT
