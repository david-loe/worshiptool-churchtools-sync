import json

import pytest

import churchtools_api
import worshiptools_api
from churchtools_api import Churchtools_API, ChurchtoolsApiError
from worshiptools_api import Worshiptools_API, WorshiptoolsApiError


class Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.content = json.dumps(self._payload).encode("utf-8")

    def json(self):
        return self._payload


class ChurchSession:
    def __init__(self, login_status=200):
        self.headers = {}
        self.login_status = login_status
        self.get_calls = []
        self.post_calls = []
        self.put_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if url.endswith("/api/whoami"):
            return Response(self.login_status, {"data": {"id": 1, "email": "a@example.test"}})
        if url.endswith("/api/csrftoken"):
            return Response(200, {"data": "csrf"})
        return Response(200, {"data": [], "meta": {"pagination": {"lastPage": 1}}})

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return Response(200, {"data": {"id": 1}})

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        return Response(200, {"data": {"id": 1}})


def test_churchtools_constructor_raises_without_auth():
    with pytest.raises(ChurchtoolsApiError):
        Churchtools_API("https://example.church.tools")


def test_churchtools_constructor_raises_on_failed_login(monkeypatch):
    monkeypatch.setattr(churchtools_api.requests, "Session", lambda: ChurchSession(login_status=401))

    with pytest.raises(ChurchtoolsApiError):
        Churchtools_API("https://example.church.tools", "bad")


def test_churchtools_get_all_raises_on_missing_pagination():
    api = object.__new__(Churchtools_API)
    api.base_url = "https://example.church.tools"
    api.session = ChurchSession()
    api.get = lambda endpoint, params=None: {"data": []}

    with pytest.raises(ChurchtoolsApiError):
        api.get_all("songs")


def test_churchtools_create_song_handles_failed_song_creation():
    api = object.__new__(Churchtools_API)
    api.post = lambda endpoint, data: None

    assert api.create_song("Song", 1) is None


def test_churchtools_create_song_handles_failed_arrangement(monkeypatch):
    monkeypatch.setattr(churchtools_api, "send_telegram_message", lambda message: None)
    api = object.__new__(Churchtools_API)

    def post(endpoint, data):
        if endpoint == "songs":
            return {"data": {"id": 1, "name": "Song", "author": "A", "ccli": None, "arrangements": []}}
        return None

    api.post = post

    assert api.create_song("Song", 1) is None


def test_churchtools_create_agenda_item_uses_before_id():
    api = object.__new__(Churchtools_API)
    api.base_url = "https://example.church.tools"
    api.session = ChurchSession()

    assert api.create_agenda_item(99, {"type": "song"}, before_id=13) == {"data": {"id": 1}}

    url, kwargs = api.session.post_calls[0]
    assert url == "https://example.church.tools/api/events/99/agenda/items?before_id=13"
    assert json.loads(kwargs["data"]) == {"type": "song"}


def test_churchtools_update_agenda_item_uses_after_id():
    api = object.__new__(Churchtools_API)
    api.base_url = "https://example.church.tools"
    api.session = ChurchSession()

    assert api.update_agenda_item(99, 12, {"type": "song"}, after_id=11) == {"data": {"id": 1}}

    url, kwargs = api.session.put_calls[0]
    assert url == "https://example.church.tools/api/events/99/agenda/items/12?after_id=11"
    assert json.loads(kwargs["data"]) == {"type": "song"}


class WorshipSession:
    def __init__(self, token="token"):
        self.headers = {}
        self.cookies = {"weAuthToken": token}
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return Response(200, {"response": {"numFound": 0, "docs": []}})

    def post(self, url, **kwargs):
        return Response(200, {})


def test_worshiptools_login_requires_bearer_token(monkeypatch):
    monkeypatch.setattr(worshiptools_api.requests, "Session", lambda: WorshipSession(token=None))

    with pytest.raises(WorshiptoolsApiError):
        Worshiptools_API("email", "password", "account")


def test_worshiptools_get_all_paginates_by_start():
    api = object.__new__(Worshiptools_API)
    calls = []

    def get(endpoint, params=None):
        calls.append(dict(params))
        start = params["start"]
        docs = [{"id": "1"}] if start == 0 else [{"id": "2"}]
        return {"numFound": 2, "docs": docs}

    api.get = get

    assert api.get_all("song", {"rows": 1}) == {"docs": [{"id": "1"}, {"id": "2"}]}
    assert calls == [{"rows": 1, "start": 0}, {"rows": 1, "start": 1}]
