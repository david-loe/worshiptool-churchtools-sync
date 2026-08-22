"""Isolated adapter for the reverse-engineered WorshipTools browser API."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx

from ..sync.errors import AuthenticationError, SchemaDriftError
from ..sync.models import SourceEvent, SourceSong

from .http import RetryPolicy, Sleep, expect_list, expect_mapping, request_json

_PLANNING_ORIGIN = "https://planning.worshiptools.com"
_API_ORIGIN = "https://api.worship.tools"
_AUTH_ORIGIN = "https://auth.worshiptools.com"
_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0, write=20.0, pool=5.0)
# Hard resource ceiling for a single provider snapshot. With the production
# page size this permits 50,000 records and rejects corrupt totals immediately.
_MAX_PAGES = 500
_MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_MAX_SNAPSHOT_RECORDS = 50_000
_MAX_LOGIN_REDIRECTS = 5
_ALLOWED_REDIRECT_HOSTS = frozenset(
    {"planning.worshiptools.com", "auth.worshiptools.com", "api.worship.tools"}
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class WorshipToolsClient:
    def __init__(
        self,
        email: str,
        password: str,
        account_id: str,
        source_timezone: str,
        *,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not email or not password or not account_id:
            raise ValueError("WorshipTools email, password, and account ID are required")
        self._email = email
        self._password = password
        self._account_id = account_id
        self._timezone = ZoneInfo(source_timezone)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; WorshipToolSync/2.0)",
                "Accept": "application/json, text/plain, */*",
            },
        )
        self._policy = retry_policy
        self._sleep = sleep
        self._token: str | None = None
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def validate(self) -> None:
        await self._ensure_login()
        # A small authenticated request verifies account membership as well as
        # credentials; a login cookie alone is not sufficient.
        await self._get_page("service", {"rows": 1, "start": 0})

    async def list_events(self, start: datetime, end: datetime) -> Sequence[SourceEvent]:
        values = await self._get_all("service", {"rows": 100})
        events = tuple(_parse_event(value, self._timezone) for value in values)
        return tuple(
            event
            for event in events
            if any(start <= value < end for value in event.starts_at)
        )

    async def list_songs(self) -> Sequence[SourceSong]:
        return tuple(_parse_song(value) for value in await self._get_all("song", {"rows": 100}))

    async def _ensure_login(self, *, force: bool = False) -> None:
        if self._token and not force:
            return
        async with self._login_lock:
            if self._token and not force:
                return
            try:
                response = await self._login_request(
                    "GET", f"{_PLANNING_ORIGIN}/app"
                )
                response.raise_for_status()
                response = await self._login_request(
                    "POST",
                    f"{_AUTH_ORIGIN}/login",
                    data={"email": self._email, "password": self._password},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise AuthenticationError("WorshipTools login request failed") from exc
            if response.status_code not in (200, 302):
                raise AuthenticationError(
                    "WorshipTools rejected login", status_code=response.status_code
                )
            token = self._client.cookies.get("weAuthToken") or response.cookies.get("weAuthToken")
            if not token:
                raise AuthenticationError("WorshipTools login returned no authentication token")
            self._token = token

    async def _login_request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        current_method = method.upper()
        current_url = httpx.URL(url)
        current_data = data
        current_headers = dict(headers or {})
        _validate_redirect_url(current_url)

        for redirect_count in range(_MAX_LOGIN_REDIRECTS + 1):
            request = self._client.build_request(
                current_method,
                current_url,
                data=current_data,
                headers=current_headers,
            )
            response = await self._client.send(
                request, stream=True, follow_redirects=False
            )
            try:
                if response.status_code not in _REDIRECT_STATUSES:
                    return response
                location = response.headers.get("Location")
                if not location:
                    raise AuthenticationError(
                        "WorshipTools login redirect lacked a target"
                    )
                if redirect_count >= _MAX_LOGIN_REDIRECTS:
                    raise AuthenticationError(
                        "WorshipTools login exceeded redirect limit"
                    )
                target = response.url.join(location)
                _validate_redirect_url(target)
                origin_changed = _url_origin(current_url) != _url_origin(target)
                if (
                    origin_changed
                    and current_data is not None
                    and response.status_code in (307, 308)
                ):
                    # Provider-operated hosts are still distinct credential
                    # boundaries. Never replay the password form across an
                    # origin under method-preserving redirect rules.
                    raise AuthenticationError(
                        "WorshipTools login refused a cross-origin credential redirect"
                    )
                if origin_changed:
                    current_headers.pop("Authorization", None)
                    current_headers.pop("Cookie", None)
                if response.status_code == 303 or (
                    response.status_code in (301, 302)
                    and current_method == "POST"
                ):
                    current_method = "GET"
                    current_data = None
                    current_headers.pop("Content-Type", None)
                current_url = target
            finally:
                await response.aclose()
        raise AuthenticationError("WorshipTools login exceeded redirect limit")

    async def _get_page(self, endpoint: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        await self._ensure_login()
        url = f"{_API_ORIGIN}/v1/account/{self._account_id}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Origin": _PLANNING_ORIGIN,
        }
        try:
            payload = await request_json(
                self._client,
                "GET",
                url,
                headers=headers,
                params=params,
                endpoint_label=f"/v1/account/[redacted]/{endpoint}",
                policy=self._policy,
                sleep=self._sleep,
            )
        except AuthenticationError:
            # Browser tokens occasionally expire.  Exactly one fresh login and
            # one replay are allowed; never recurse indefinitely.
            await self._ensure_login(force=True)
            headers["Authorization"] = f"Bearer {self._token}"
            payload = await request_json(
                self._client,
                "GET",
                url,
                headers=headers,
                params=params,
                endpoint_label=f"/v1/account/[redacted]/{endpoint}",
                policy=self._policy,
                sleep=self._sleep,
            )
        root = expect_mapping(payload, "root")
        return expect_mapping(root.get("response"), "response")

    async def _get_all(self, endpoint: str, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        start = 0
        total = 1
        page = 0
        aggregate_bytes = 0
        raw_page_size = params.get("rows")
        if (
            not isinstance(raw_page_size, int)
            or isinstance(raw_page_size, bool)
            or raw_page_size < 1
        ):
            raise SchemaDriftError(
                "WorshipTools pagination requires a positive page size",
                endpoint=endpoint,
            )
        while start < total:
            page += 1
            if page > _MAX_PAGES:
                raise SchemaDriftError("WorshipTools pagination exceeded safety limit", endpoint=endpoint)
            response = await self._get_page(endpoint, {**params, "start": start})
            docs = expect_list(response.get("docs"), "response.docs")
            raw_total = response.get("numFound")
            if not isinstance(raw_total, int) or isinstance(raw_total, bool) or raw_total < 0:
                raise SchemaDriftError("WorshipTools response lacks a valid numFound", endpoint=endpoint)
            total = raw_total
            if len(docs) > raw_page_size:
                raise SchemaDriftError(
                    "WorshipTools page exceeded the requested size",
                    endpoint=endpoint,
                )
            if (
                total > raw_page_size * _MAX_PAGES
                or total > _MAX_SNAPSHOT_RECORDS
                or len(result) + len(docs) > _MAX_SNAPSHOT_RECORDS
            ):
                raise SchemaDriftError(
                    "WorshipTools pagination exceeded safety limit",
                    endpoint=endpoint,
                    total=total,
                )
            page_bytes = json.dumps(
                docs, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
            aggregate_bytes += len(page_bytes)
            if aggregate_bytes > _MAX_SNAPSHOT_BYTES:
                raise SchemaDriftError(
                    "WorshipTools snapshot exceeded the aggregate size limit",
                    endpoint=endpoint,
                )
            if total > start and not docs:
                raise SchemaDriftError(
                    "WorshipTools pagination returned an empty page before numFound", endpoint=endpoint, start=start
                )
            if len(result) + len(docs) > total:
                raise SchemaDriftError(
                    "WorshipTools page exceeded numFound",
                    endpoint=endpoint,
                    start=start,
                )
            result.extend(expect_mapping(value, "response.docs[]") for value in docs)
            next_start = len(result)
            if next_start <= start and next_start < total:
                raise SchemaDriftError("WorshipTools pagination made no progress", endpoint=endpoint, start=start)
            start = next_start
        return result


def _parse_event(value: Any, zone: ZoneInfo) -> SourceEvent:
    item = expect_mapping(value, "service")
    event_id = item.get("id", item.get("_id"))
    if event_id is None:
        raise SchemaDriftError("WorshipTools service lacks id")
    raw_times = expect_list(item.get("times"), "service.times")
    starts = tuple(_parse_datetime(value, zone) for value in raw_times)
    raw_songs = expect_list(item.get("songs") or [], "service.songs")
    song_ids: list[str] = []
    for raw_song in raw_songs:
        if isinstance(raw_song, (str, int)):
            song_ids.append(str(raw_song))
            continue
        song = expect_mapping(raw_song, "service.songs[]")
        song_id = song.get("id", song.get("_id", song.get("songId")))
        if song_id is None:
            raise SchemaDriftError("WorshipTools service song lacks id")
        song_ids.append(str(song_id))
    return SourceEvent(
        id=str(event_id),
        name=str(item.get("name") or ""),
        starts_at=starts,
        song_ids=tuple(song_ids),
    )


def _validate_redirect_url(url: httpx.URL) -> None:
    if (
        url.scheme != "https"
        or url.host not in _ALLOWED_REDIRECT_HOSTS
        or url.port not in (None, 443)
        or url.userinfo
    ):
        raise AuthenticationError("WorshipTools login redirect target is not allowed")


def _url_origin(url: httpx.URL) -> tuple[str, str, int]:
    return (url.scheme, url.host, url.port or 443)


def _parse_song(value: Any) -> SourceSong:
    item = expect_mapping(value, "song")
    song_id = item.get("id", item.get("_id"))
    if song_id is None or not isinstance(item.get("name"), str):
        raise SchemaDriftError("WorshipTools song lacks id or name")
    return SourceSong(
        id=str(song_id),
        name=item["name"],
        artist=str(item.get("artist") or item.get("author") or ""),
        ccli=str(item["ccli"]) if item.get("ccli") not in (None, "") else None,
    )


def _parse_datetime(value: Any, zone: ZoneInfo) -> datetime:
    if isinstance(value, Mapping):
        value = value.get("start", value.get("time"))
    if not isinstance(value, str):
        raise SchemaDriftError("WorshipTools service time is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaDriftError("WorshipTools service time is not ISO-8601") from exc
    if parsed.tzinfo is None:
        # Reverse-engineered WT timestamps are wall-clock values in the
        # account timezone.  Make that assumption explicit at the adapter edge.
        parsed = parsed.replace(tzinfo=zone)
    return parsed
