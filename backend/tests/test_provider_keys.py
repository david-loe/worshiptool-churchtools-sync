from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
import json

import httpx
import pytest

from app.providers.churchtools import ChurchToolsClient, _parse_agenda_item
from app.providers.http import RetryPolicy
from app.providers.worshiptools import WorshipToolsClient
import app.providers.worshiptools as wt
from app.sync.errors import AuthenticationError, AuthorizationError, SchemaDriftError, SyncError
from app.sync.models import SourceEvent, SourceSong


def firestore(value):
    if isinstance(value, dict):
        return {"mapValue": {"fields": {key: firestore(item) for key, item in value.items()}}}
    if isinstance(value, list):
        return {"arrayValue": {"values": [firestore(item) for item in value]}}
    if value is None:
        return {"nullValue": None}
    return {"stringValue": value}


def source_fixture(*, service_status=200, service_data=None, chart_status=200, chart_key="G", expire=False, envelope=True):
    calls = Counter()

    def handler(request):
        path = request.url.path
        calls[path] += 1
        if path in {"/v1/config/firebase", "/v1/firebase/token"}:
            assert request.headers["Authorization"] == "Bearer planning-secret"
            value = {"apiKey": "api-secret", "projectId": "project"} if path.endswith("firebase") else {"token": "custom-secret"}
            return httpx.Response(200, json={"response": value} if envelope else value)
        if request.url.host == "identitytoolkit.googleapis.com":
            assert "Authorization" not in request.headers
            assert json.loads(request.content) == {"token": "custom-secret", "returnSecureToken": True}
            assert request.url.params["key"] == "api-secret"
            return httpx.Response(200, json={"idToken": "firestore-secret"})
        assert request.url.host == "firestore.googleapis.com"
        assert request.headers["Authorization"] == "Bearer firestore-secret"
        if "/cuelists/" in path:
            if expire and calls[path] == 1:
                return httpx.Response(401)
            return httpx.Response(service_status, json={"fields": firestore({"data": service_data or {}})["mapValue"]["fields"]})
        assert path.endswith("/songs/s")
        return httpx.Response(chart_status, json={"fields": firestore({"data": {"defaultKey": chart_key}})["mapValue"]["fields"]})

    client = WorshipToolsClient("a@example.test", "password", "account", "UTC",
                                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                retry_policy=RetryPolicy(attempts=1))
    client._token = "planning-secret"
    event = SourceEvent("service", "Service", (datetime(2026, 9, 20, tzinfo=timezone.utc),), ("s",))
    songs = (SourceSong("s", "Song", "Artist", key="C"), SourceSong("unused", "Unused", "Artist", key="F"))
    return client, event, songs, calls


def test_firestore_cue_keys_preserve_order_duplicates_and_cache_documents(caplog):
    client, event, songs, calls = source_fixture(service_data={
        "cues": [
            {"cue_id": "a", "cue_foreground": {"type": "text", "text": "s"}},
            {"cue_id": "blank", "cue_foreground": {"type": "video", "text": "s"}},
            {"cue_id": "b", "cue_foreground": {"type": "text", "text": "s"}},
            {"cue_id": "c", "cue_foreground": {"type": "text", "text": "s"}},
        ],
        "songMeta": {"a": {"key": "D"}, "b": {"key": "F#"}},
    })
    events, enriched_songs = asyncio.run(client.enrich_snapshot((event, event), songs))
    assert events[0].song_ids == ("s", "s", "s")
    assert events[0].song_keys == ("D", "F#", "G")
    assert events[1] == events[0]
    assert enriched_songs[0].key == "G"
    assert enriched_songs[1] == songs[1]
    assert all(count == 1 for count in calls.values())
    assert not any(secret in caplog.text for secret in ("planning-secret", "api-secret", "custom-secret", "firestore-secret"))


@pytest.mark.parametrize(("service_status", "chart_status", "chart_key", "expected"), [
    (404, 200, "G", "G"), (200, 404, "G", "C"), (200, 200, "  ", "C"),
    (404, 404, None, "C"), (200, 200, "invalid", "invalid"),
])
def test_missing_documents_and_empty_fields_fall_back_but_invalid_keys_are_retained(service_status, chart_status, chart_key, expected):
    client, event, songs, _ = source_fixture(service_status=service_status, chart_status=chart_status, chart_key=chart_key)
    events, _ = asyncio.run(client.enrich_snapshot((event,), songs))
    assert events[0].song_keys == (expected,)


@pytest.mark.parametrize(("status", "error"), [(401, AuthenticationError), (403, AuthorizationError), (500, SyncError)])
def test_firestore_read_errors_do_not_silently_fall_back(status, error):
    client, event, songs, _ = source_fixture(service_status=status)
    with pytest.raises(error):
        asyncio.run(client.enrich_snapshot((event,), songs))


def test_expired_firestore_token_is_refreshed_once():
    client, event, songs, calls = source_fixture(expire=True)
    events, _ = asyncio.run(client.enrich_snapshot((event,), songs))
    assert events[0].song_keys == ("G",)
    assert calls["/v1/accounts:signInWithCustomToken"] == 2


def test_direct_bootstrap_responses_and_null_optional_service_metadata():
    client, event, songs, _ = source_fixture(envelope=False, service_data={"songMeta": None, "cues": None})
    events, _ = asyncio.run(client.enrich_snapshot((event,), songs))
    assert events[0].song_keys == ("G",)


@pytest.mark.parametrize("data", [{"songMeta": []}, {"cues": "wrong"}, {"cues": [{"cue_foreground": []}]}])
def test_malformed_firestore_documents_fail(data):
    client, event, songs, _ = source_fixture(service_data=data)
    with pytest.raises(SchemaDriftError):
        asyncio.run(client.enrich_snapshot((event,), songs))


def test_firestore_snapshot_has_aggregate_size_and_record_limits(monkeypatch):
    for setting in ("_MAX_SNAPSHOT_BYTES", "_MAX_SNAPSHOT_RECORDS"):
        with monkeypatch.context() as context:
            context.setattr(wt, setting, 1)
            client, event, songs, _ = source_fixture()
            with pytest.raises(SchemaDriftError, match="limit"):
                asyncio.run(client.enrich_snapshot((event,), songs))


def test_empty_event_snapshot_needs_no_firebase_requests():
    client, _, songs, calls = source_fixture()
    assert asyncio.run(client.enrich_snapshot((), songs)) == ((), songs)
    assert not calls


@pytest.mark.parametrize("outcome", ["success", "timeout", "malformed-response"])
def test_arrangement_writes_send_key_without_changing_default_and_reconcile(outcome):
    arrangements = [{"id": 1, "name": "Original", "isDefault": True, "key": "C"}]
    writes = []

    def handler(request):
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"id": 7, "name": "Song", "arrangements": arrangements}})
        body = json.loads(request.content)
        writes.append(body)
        arrangements.append({"id": 2, "isDefault": False, **body})
        if outcome == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if outcome == "malformed-response":
            return httpx.Response(201, json={"data": {}})
        return httpx.Response(201, json={"data": arrangements[-1]})

    client = ChurchToolsClient("https://demo.church.tools", "token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    first = asyncio.run(client.create_arrangement("7", "Standard (D)", "action", key="D"))
    again = asyncio.run(client.create_arrangement("7", "Different name", "retry", key="D"))
    assert first.id == again.id == "2"
    assert first.key == "D"
    assert writes == [{"name": "Standard (D)", "key": "D"}]
    assert arrangements[0] == {"id": 1, "name": "Original", "isDefault": True, "key": "C"}


def test_song_creation_sends_inline_key_and_reconciles_the_matching_arrangement():
    songs = []
    writes = []

    def handler(request):
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": songs, "meta": {"pagination": {"lastPage": 1}}})
        body = json.loads(request.content)
        writes.append(body)
        songs.append({"id": 7, **body})
        songs[0]["arrangements"][0]["id"] = 8
        raise httpx.ReadTimeout("response lost", request=request)

    client = ChurchToolsClient("https://demo.church.tools", "token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    payload = {"name": "Song", "author": "Artist", "ccli": "123", "category_id": 1, "arrangement_name": "Standard (Db)", "key": "Db"}
    song = asyncio.run(client.create_song(payload, "action"))
    assert song.arrangements[0].key == "Db"
    assert writes[0]["arrangements"][0]["key"] == "Db"
    assert len(writes) == 1
    # Same song/name with an incorrect key is not successful reconciliation.
    songs[0]["arrangements"][0]["key"] = "D"
    with pytest.raises(SchemaDriftError):
        asyncio.run(client.create_song(payload, "retry"))
    assert len(writes) == 1


def test_agenda_parser_reads_the_arrangement_key():
    item = _parse_agenda_item({"id": 1, "type": "song", "song": {"songId": 7, "arrangementId": 8, "key": "D"}}, 0)
    assert item.key == "D"
