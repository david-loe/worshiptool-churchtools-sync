from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

import app.providers.churchtools as churchtools_module
import app.providers.worshiptools as worshiptools_module
from app.providers.churchtools import ChurchToolsClient
from app.providers.http import RetryPolicy, request_json
from app.providers.worshiptools import WorshipToolsClient
from app.sync.errors import (
    AuthenticationError,
    IndeterminateWriteError,
    RateLimitError,
    SchemaDriftError,
    SyncError,
)


def run(coro):
    return asyncio.run(coro)


def test_churchtools_rejects_non_tenant_or_non_https_urls() -> None:
    with pytest.raises(ValueError):
        ChurchToolsClient("http://demo.church.tools", "token")
    with pytest.raises(ValueError):
        ChurchToolsClient("https://church.tools", "token")
    with pytest.raises(ValueError):
        ChurchToolsClient("https://demo.church.tools.evil.test", "token")


def test_churchtools_whoami_is_strict_and_events_need_no_pagination_meta() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/whoami":
            # E-mail is optional in the documented whoami response.
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/events":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 3,
                            "name": "Outside",
                            "startDate": "2025-12-31T10:00:00Z",
                            "calendar": {
                                "domainIdentifier": "2",
                                "domainAttributes": {"campusName": "Nord"},
                            },
                        },
                        {
                            "id": 4,
                            "name": "Gottesdienst",
                            "startDate": "2026-01-01T10:00:00Z",
                            "calendar": {
                                "domainIdentifier": "2",
                                "domainAttributes": {"campusName": "Nord"},
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/api/calendars":
            return httpx.Response(
                200,
                json={"data": [{"id": 2, "name": "Gottesdienst", "campusId": 3}]},
            )
        raise AssertionError(request.url)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = ChurchToolsClient("https://demo.church.tools", "secret", client=http)

    events = run(client.list_events(_date("2026-01-01T00:00:00Z"), _date("2026-01-02T00:00:00Z")))

    assert [event.id for event in events] == ["4"]
    assert events[0].calendar_id == "2"
    assert events[0].campus_id == "3"
    assert events[0].campus_name == "Nord"
    whoami = requests[0]
    assert whoami.headers["Authorization"] == "Login secret"
    assert whoami.url.params["only_allow_authenticated"] == "true"
    event_request = next(
        request for request in requests if request.url.path == "/api/events"
    )
    assert event_request.url.params["from"] == "2025-12-31"
    # ChurchTools treats `to` as exclusive; the adapter deliberately widens
    # the local calendar range and filters instants after parsing.
    assert event_request.url.params["to"] == "2026-01-04"
    assert event_request.url.params["limit"] == "100"


def test_churchtools_events_follow_documented_pagination() -> None:
    event_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/calendars":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/api/events":
            page = int(request.url.params["page"])
            event_pages.append(page)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": page,
                            "name": f"Event {page}",
                            "startDate": f"2026-01-0{page}T10:00:00Z",
                            "calendar": {},
                        }
                    ],
                    "meta": {"pagination": {"lastPage": 2}},
                },
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    events = run(
        client.list_events(
            _date("2026-01-01T00:00:00Z"),
            _date("2026-01-03T00:00:00Z"),
        )
    )

    assert event_pages == [1, 2]
    assert [event.id for event in events] == ["1", "2"]


def test_churchtools_rejects_unbounded_pagination_before_following_it() -> None:
    song_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal song_requests
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/songs":
            song_requests += 1
            return httpx.Response(
                200,
                json={"data": [], "meta": {"pagination": {"lastPage": 501}}},
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SchemaDriftError, match="safety limit"):
        run(client.list_songs())

    assert song_requests == 1


def test_churchtools_rejects_page_larger_than_requested_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/songs":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": index, "name": f"Song {index}", "arrangements": []}
                        for index in range(101)
                    ],
                    "meta": {"pagination": {"lastPage": 1}},
                },
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SchemaDriftError, match="requested limit"):
        run(client.list_songs())


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "songs", "error_text"),
    [
        (
            "_MAX_SNAPSHOT_RECORDS",
            1,
            [
                {"id": 1, "name": "One", "arrangements": []},
                {"id": 2, "name": "Two", "arrangements": []},
            ],
            "record limit",
        ),
        (
            "_MAX_SNAPSHOT_BYTES",
            32,
            [{"id": 1, "name": "x" * 100, "arrangements": []}],
            "aggregate size limit",
        ),
    ],
)
def test_churchtools_snapshot_has_aggregate_record_and_byte_limits(
    monkeypatch,
    limit_name: str,
    limit_value: int,
    songs: list[dict],
    error_text: str,
) -> None:
    monkeypatch.setattr(churchtools_module, limit_name, limit_value)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/songs":
            return httpx.Response(
                200,
                json={
                    "data": songs,
                    "meta": {"pagination": {"lastPage": 1}},
                },
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SchemaDriftError, match=error_text):
        run(client.list_songs())


def test_churchtools_song_list_requests_arrangements() -> None:
    seen_include: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1, "email": "a@example.test"}})
        if request.url.path == "/api/songs":
            seen_include.append(request.url.params.get_list("include"))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 1,
                            "name": "Song",
                            "author": "A",
                            "ccli": "4",
                            "arrangements": [{"id": 2, "name": "Standard", "isDefault": True}],
                        }
                    ],
                    "meta": {"pagination": {"lastPage": 1}},
                },
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    songs = run(client.list_songs())

    assert seen_include == [["arrangements"]]
    assert songs[0].arrangements[0].id == "2"


def test_churchtools_get_song_explicitly_requests_arrangements() -> None:
    includes: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/songs/1":
            includes.append(request.url.params.get_list("include"))
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": 1,
                        "name": "Song",
                        "arrangements": [
                            {"id": 2, "name": "Standard", "isDefault": True}
                        ],
                    }
                },
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    song = run(client.get_song("1"))

    assert includes == [["arrangements"]]
    assert song.arrangements[0].id == "2"


def test_churchtools_metadata_uses_supported_endpoints_and_song_categories() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/calendars":
            return httpx.Response(200, json={"data": [{"id": 2, "name": "Gottesdienst"}]})
        if request.url.path == "/api/campuses":
            return httpx.Response(200, json={"data": [{"id": 3, "name": "Nord"}]})
        if request.url.path == "/api/event/masterdata":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "songCategories": [
                            {"id": 8, "name": "Noch unbenutzt", "campusId": None},
                            {"id": 7, "name": "Lobpreis", "campusId": 3},
                        ]
                    },
                    "meta": {},
                },
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    metadata = run(client.metadata())

    assert metadata == {
        "calendars": [{"id": "2", "name": "Gottesdienst"}],
        "campuses": [{"id": "3", "name": "Nord"}],
        "song_categories": [
            {"id": "7", "name": "Lobpreis"},
            {"id": "8", "name": "Noch unbenutzt"},
        ],
    }
    assert "/api/event/masterdata" in paths
    assert "/api/songs" not in paths


def test_churchtools_reads_agenda_arrangement_from_nested_song_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1, "email": "a@example.test"}})
        if request.url.path == "/api/events/4/agenda":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "id": 9,
                                "position": 0,
                                "type": "song",
                                "song": {"songId": 7, "arrangementId": 8},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(request.url)

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    agenda = run(client.get_agenda("4"))

    assert agenda.items[0].song_id == "7"
    assert agenda.items[0].arrangement_id == "8"


def test_churchtools_create_song_sends_inline_default_arrangement() -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1, "email": "a@example.test"}})
        if request.url.path == "/api/songs" and request.method == "GET":
            return httpx.Response(200, json={"data": [], "meta": {"pagination": {"lastPage": 1}}})
        if request.url.path == "/api/songs" and request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": 7,
                        "name": "New",
                        "author": "Artist",
                        "ccli": "123",
                        "arrangements": [{"id": 8, "name": "Standard", "isDefault": True}],
                    }
                },
            )
        raise AssertionError((request.method, request.url))

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    song = run(
        client.create_song(
            {
                "name": "New",
                "author": "Artist",
                "ccli": "123",
                "category_id": 9,
                "arrangement_name": "Standard",
            },
            "action",
        )
    )

    assert song.arrangements[0].id == "8"
    assert posted[0]["arrangements"] == [{"name": "Standard", "isDefault": True}]


def test_churchtools_agenda_write_sends_numeric_arrangement_id() -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/events/4/agenda" and request.method == "GET":
            return httpx.Response(200, json={"data": {"items": []}})
        if request.url.path == "/api/events/4/agenda/items" and request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": 9,
                        "position": 0,
                        "type": "song",
                        "song": {"songId": 7, "arrangementId": 8},
                    }
                },
            )
        raise AssertionError((request.method, request.url))

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    item = run(
        client.insert_agenda_song(
            "4",
            "8",
            {
                "title": "Lobpreis",
                "note": "Direkt beginnen",
                "responsible": "[Worship Leader]",
                "duration": 300,
            },
            "action",
        )
    )

    assert item.arrangement_id == "8"
    assert posted[0] == {
        "type": "song",
        "title": "Lobpreis",
        "note": "Direkt beginnen",
        "responsible": "[Worship Leader]",
        "duration": 300,
        "arrangementId": 8,
    }
    assert isinstance(posted[0]["arrangementId"], int)


@pytest.mark.parametrize("write_response", ["empty", "invalid", "schema"])
def test_churchtools_reconciles_committed_insert_with_unusable_success_body(
    write_response: str,
) -> None:
    agenda_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal agenda_reads
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/events/4/agenda" and request.method == "GET":
            agenda_reads += 1
            if agenda_reads == 1:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "items": [
                                {"id": "header", "position": 0, "type": "header"},
                                {"id": "text", "position": 1, "type": "text"},
                            ]
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {"id": "header", "position": 0, "type": "header"},
                            {
                                "id": "committed",
                                "position": 1,
                                "type": "song",
                                "song": {"songId": 7, "arrangementId": 8},
                            },
                            {"id": "text", "position": 2, "type": "text"},
                        ]
                    }
                },
            )
        if request.url.path == "/api/events/4/agenda/items" and request.method == "POST":
            assert request.url.params["before_id"] == "text"
            if write_response == "empty":
                return httpx.Response(201, content=b"")
            if write_response == "invalid":
                return httpx.Response(201, content=b"{")
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": "committed",
                        "type": "song",
                        "song": {},
                    }
                },
            )
        raise AssertionError((request.method, request.url))

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    item = run(
        client.insert_agenda_song(
            "4", "8", {}, "action", before_item_id="text"
        )
    )

    assert item.id == "committed"
    assert item.song_id == "7"
    assert agenda_reads == 2


def test_churchtools_does_not_claim_same_arrangement_at_wrong_position() -> None:
    agenda_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal agenda_reads
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"data": {"id": 1}})
        if request.url.path == "/api/events/4/agenda" and request.method == "GET":
            agenda_reads += 1
            items = [
                {"id": "header", "position": 0, "type": "header"},
                {"id": "text", "position": 1, "type": "text"},
            ]
            if agenda_reads > 1:
                # A human inserted the same arrangement, but not at the position
                # requested by our indeterminate write.
                items.append(
                    {
                        "id": "human",
                        "position": 2,
                        "type": "song",
                        "song": {"songId": 7, "arrangementId": 8},
                    }
                )
            return httpx.Response(200, json={"data": {"items": items}})
        if request.url.path == "/api/events/4/agenda/items" and request.method == "POST":
            return httpx.Response(201, content=b"{")
        raise AssertionError((request.method, request.url))

    client = ChurchToolsClient(
        "https://demo.church.tools",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(IndeterminateWriteError):
        run(
            client.insert_agenda_song(
                "4", "8", {}, "action", before_item_id="text"
            )
        )


def test_request_json_accepts_empty_204_and_retries_safe_get() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.method == "DELETE":
            return httpx.Response(204)
        if calls < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert run(request_json(client, "DELETE", "https://example.test/item", write=True)) is None
    calls = 0
    result = run(
        request_json(
            client,
            "GET",
            "https://example.test/item",
            policy=RetryPolicy(attempts=3, base_delay=0, max_delay=0),
            sleep=lambda delay: _record_sleep(sleeps, delay),
        )
    )
    assert result == {"ok": True}
    assert calls == 3
    assert len(sleeps) == 2


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "content_length",
    ["not-a-number", str(9 * 1024 * 1024), "9" * 5_000],
)
def test_request_json_rejects_invalid_or_oversized_content_length_and_closes(
    content_length: str,
) -> None:
    stream = TrackingStream([b'{"ok":true}'])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": content_length},
            stream=stream,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SchemaDriftError):
        run(request_json(client, "GET", "https://example.test/data"))
    assert stream.closed is True


def test_request_json_rejects_content_length_mismatch() -> None:
    stream = TrackingStream([b"{}"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "3"},
            stream=stream,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SchemaDriftError, match="Content-Length"):
        run(request_json(client, "GET", "https://example.test/data"))
    assert stream.closed is True


def test_request_json_rejects_oversized_chunked_body_and_closes() -> None:
    stream = TrackingStream([b"x" * (4 * 1024 * 1024)] * 3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SchemaDriftError, match="size limit"):
        run(request_json(client, "GET", "https://example.test/data"))
    assert stream.closed is True


def test_request_json_does_not_obey_unbounded_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "3600"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RateLimitError) as error:
        run(
            request_json(
                client,
                "GET",
                "https://example.test/data",
                policy=RetryPolicy(attempts=4, max_delay=4),
                sleep=lambda delay: _record_sleep(sleeps, delay),
            )
        )

    assert error.value.retry_after == 3600
    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize("retry_after", ["NaN", "Infinity", "-Infinity"])
def test_retry_after_rejects_non_finite_numbers(retry_after: str) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = run(
        request_json(
            client,
            "GET",
            "https://example.test/data",
            policy=RetryPolicy(attempts=2, base_delay=0, max_delay=0),
            sleep=lambda delay: _record_sleep(sleeps, delay),
        )
    )

    assert result == {"ok": True}
    assert calls == 2
    assert sleeps == [0]


def test_worshiptools_rejects_foreign_307_before_replaying_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL("https://planning.worshiptools.com/app"):
            return httpx.Response(200)
        if request.url == httpx.URL("https://auth.worshiptools.com/login"):
            return httpx.Response(307, headers={"Location": "https://evil.test/capture"})
        raise AssertionError(f"foreign redirect was followed: {request.url}")

    client = WorshipToolsClient(
        "mail@example.test",
        "password-secret",
        "account",
        "Europe/Berlin",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )

    with pytest.raises(AuthenticationError, match="target is not allowed"):
        run(client._ensure_login())

    assert [request.url.host for request in requests] == [
        "planning.worshiptools.com",
        "auth.worshiptools.com",
    ]


def test_worshiptools_never_replays_password_to_another_allowed_origin() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL("https://planning.worshiptools.com/app"):
            return httpx.Response(200)
        if request.url == httpx.URL("https://auth.worshiptools.com/login"):
            return httpx.Response(
                307,
                headers={
                    "Location": "https://planning.worshiptools.com/session"
                },
            )
        raise AssertionError(f"credential redirect was followed: {request.url}")

    client = WorshipToolsClient(
        "mail@example.test",
        "never-forward-this-password",
        "account",
        "Europe/Berlin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(AuthenticationError, match="credential redirect"):
        run(client._ensure_login())

    assert [request.url.host for request in requests] == [
        "planning.worshiptools.com",
        "auth.worshiptools.com",
    ]
    assert b"never-forward-this-password" in requests[-1].content


def test_worshiptools_bounds_allowed_redirect_loops() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL("https://planning.worshiptools.com/app"):
            return httpx.Response(200)
        return httpx.Response(
            302, headers={"Location": "https://auth.worshiptools.com/loop"}
        )

    client = WorshipToolsClient(
        "mail@example.test",
        "password",
        "account",
        "Europe/Berlin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(AuthenticationError, match="redirect limit"):
        run(client._ensure_login())

    assert len(requests) == 7


def test_worshiptools_follows_bounded_allowed_login_redirect_without_post_replay() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL("https://planning.worshiptools.com/app"):
            return httpx.Response(200)
        if request.url == httpx.URL("https://auth.worshiptools.com/login"):
            return httpx.Response(
                302,
                headers={
                    "Location": "https://planning.worshiptools.com/login-complete",
                    "Set-Cookie": "weAuthToken=safe-token; Path=/; Secure",
                },
            )
        if request.url == httpx.URL(
            "https://planning.worshiptools.com/login-complete"
        ):
            assert request.method == "GET"
            assert request.content == b""
            return httpx.Response(200)
        raise AssertionError(request.url)

    client = WorshipToolsClient(
        "mail@example.test",
        "password",
        "account",
        "Europe/Berlin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    run(client._ensure_login())

    assert client._token == "safe-token"
    assert len(requests) == 3


def test_worshiptools_empty_page_before_total_is_schema_drift() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": {"numFound": 2, "docs": []}})

    client = WorshipToolsClient(
        "mail@example.test",
        "password",
        "account",
        "Europe/Berlin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client._token = "token"

    with pytest.raises(SchemaDriftError):
        run(client.list_songs())


def test_worshiptools_rejects_unbounded_total_before_following_it() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "response": {
                    "numFound": 50_001,
                    "docs": [{"id": "song", "name": "Song"}],
                }
            },
        )

    client = WorshipToolsClient(
        "mail@example.test",
        "password",
        "account",
        "Europe/Berlin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client._token = "token"

    with pytest.raises(SchemaDriftError, match="safety limit"):
        run(client.list_songs())

    assert requests == 1


def test_worshiptools_redacts_account_id_from_public_http_errors() -> None:
    account_id = "private-account-id"

    def handler(request: httpx.Request) -> httpx.Response:
        assert account_id in request.url.path
        return httpx.Response(503, json={})

    client = WorshipToolsClient(
        "mail@example.test",
        "password",
        account_id,
        "Europe/Berlin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(attempts=1),
    )
    client._token = "token"

    with pytest.raises(SyncError) as error:
        run(client.list_songs())

    serialized = json.dumps(error.value.as_dict())
    assert account_id not in serialized
    assert error.value.details["endpoint"] == "/v1/account/[redacted]/song"
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_worshiptools_snapshot_has_aggregate_byte_limit(monkeypatch) -> None:
    monkeypatch.setattr(worshiptools_module, "_MAX_SNAPSHOT_BYTES", 32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": {
                    "numFound": 1,
                    "docs": [{"id": "song", "name": "x" * 100}],
                }
            },
        )

    client = WorshipToolsClient(
        "mail@example.test",
        "password",
        "account",
        "Europe/Berlin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client._token = "token"

    with pytest.raises(SchemaDriftError, match="aggregate size limit"):
        run(client.list_songs())


async def _record_sleep(values: list[float], delay: float) -> None:
    values.append(delay)


def _date(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
