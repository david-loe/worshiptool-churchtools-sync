"""Typed async adapter for the ChurchTools operations used by the sync."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from ..sync.errors import ConflictError, IndeterminateWriteError, SchemaDriftError
from ..sync.matching import match_song, normalize_text
from ..sync.models import (
    Agenda,
    AgendaItem,
    Arrangement,
    SourceSong,
    TargetEvent,
    TargetSong,
)

from .http import RetryPolicy, Sleep, expect_list, expect_mapping, request_json

_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0, write=20.0, pool=5.0)
# Hard resource ceiling for a single provider snapshot. At 100 rows per page
# this still allows 50,000 records while failing fast on pagination drift.
_MAX_PAGES = 500
_MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_MAX_SNAPSHOT_RECORDS = 50_000


class ChurchToolsClient:
    def __init__(
        self,
        base_url: str,
        login_token: str,
        target_timezone: str = "UTC",
        *,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self._timezone = ZoneInfo(target_timezone)
        if not login_token:
            raise ValueError("ChurchTools login token must not be empty")
        self._headers = {"Authorization": f"Login {login_token}", "Accept": "application/json"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, follow_redirects=False)
        self._policy = retry_policy
        self._sleep = sleep
        self._validated = False
        self._validation_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def validate(self) -> Mapping[str, Any]:
        payload = await self._get("whoami", params={"only_allow_authenticated": "true"}, ensure_auth=False)
        data = expect_mapping(expect_mapping(payload, "root").get("data"), "data")
        if not data.get("id"):
            raise SchemaDriftError("ChurchTools whoami response lacks authenticated identity")
        self._validated = True
        return data

    async def list_events(self, start: datetime, end: datetime) -> Sequence[TargetEvent]:
        await self._ensure_validated()
        # The date-range variant is documented as returning the complete range
        # and some installations omit pagination metadata for it.
        local_from = start.astimezone(self._timezone).date() - timedelta(days=1)
        # CT documents `to` as exclusive. The safety margin also absorbs
        # installation/profile timezone drift around DST and midnight.
        local_to = end.astimezone(self._timezone).date() + timedelta(days=2)
        event_values, calendars_payload = await asyncio.gather(
            self._get_all(
                "events",
                {
                    "from": local_from.isoformat(),
                    "to": local_to.isoformat(),
                    "limit": 100,
                },
                allow_missing_pagination=True,
            ),
            self._get("calendars"),
        )
        calendar_campuses = _calendar_campus_ids(calendars_payload)
        parsed = tuple(
            _parse_event(value, calendar_campuses) for value in event_values
        )
        return tuple(event for event in parsed if start <= event.starts_at < end)

    async def list_songs(self) -> Sequence[TargetSong]:
        await self._ensure_validated()
        values = await self._get_all(
            "songs", {"limit": 100, "include": ["arrangements"]}
        )
        return tuple(_parse_song(value, require_arrangements=True) for value in values)

    async def metadata(self) -> dict[str, list[dict[str, str]]]:
        """Return only selector metadata consumed by the profile editor."""

        await self._ensure_validated()
        calendars_response, campuses_response, masterdata_response = await asyncio.gather(
            self._get("calendars"),
            self._get("campuses"),
            self._get("event/masterdata"),
        )
        calendars_payload = expect_mapping(calendars_response, "root")
        campuses_payload = expect_mapping(campuses_response, "root")
        masterdata_payload = expect_mapping(masterdata_response, "root")
        masterdata = expect_mapping(masterdata_payload.get("data"), "data")
        calendars = [
            _named_metadata(value, "calendar")
            for value in expect_list(calendars_payload.get("data"), "data")
        ]
        campuses = [
            _named_metadata(value, "campus")
            for value in expect_list(campuses_payload.get("data"), "data")
        ]
        categories = [
            _named_metadata(value, "song category")
            for value in expect_list(
                masterdata.get("songCategories"), "data.songCategories"
            )
        ]
        return {
            "calendars": sorted(calendars, key=lambda value: (value["name"], value["id"])),
            "campuses": sorted(campuses, key=lambda value: (value["name"], value["id"])),
            "song_categories": sorted(
                categories, key=lambda value: (value["name"], value["id"])
            ),
        }

    async def get_song(self, song_id: str) -> TargetSong:
        await self._ensure_validated()
        payload = await self._get(
            f"songs/{song_id}", {"include": ["arrangements"]}
        )
        return _parse_song(
            expect_mapping(payload, "root").get("data"),
            require_arrangements=True,
        )

    async def get_agenda(self, event_id: str) -> Agenda:
        await self._ensure_validated()
        payload = await self._get(f"events/{event_id}/agenda")
        data = expect_mapping(expect_mapping(payload, "root").get("data"), "data")
        return Agenda(str(event_id), tuple(_parse_agenda_item(value, index) for index, value in enumerate(expect_list(data.get("items"), "items"))))

    async def create_song(self, payload: Mapping[str, Any], action_id: str) -> TargetSong:
        """Create song and default arrangement in one ChurchTools request."""

        await self._ensure_validated()
        existing = await self._find_reconciled_song(payload)
        if existing is not None:
            return existing
        body = {
            "name": payload["name"],
            "categoryId": payload["category_id"],
            "author": payload.get("author") or "",
            "ccli": payload.get("ccli") or None,
            "arrangements": [{"name": payload["arrangement_name"], "isDefault": True}],
        }
        try:
            response = await self._write("POST", "songs", body)
            song = _parse_song(expect_mapping(response, "root").get("data"))
            if not song.arrangements:
                # The create representation may omit included resources even
                # though the inline write was atomic. Re-read before judging
                # the contract instead of attempting a second write.
                song = await self.get_song(song.id)
        except (IndeterminateWriteError, SchemaDriftError):
            song = await self._find_reconciled_song(payload)
            if song is None:
                raise
        if not song.arrangements:
            # The atomic endpoint contract was not honoured; do not report a
            # partially-created song as success.
            raise SchemaDriftError("ChurchTools created a song without the inline default arrangement")
        return song

    async def create_arrangement(self, song_id: str, name: str, action_id: str) -> Arrangement:
        await self._ensure_validated()
        song = await self.get_song(song_id)
        matches = [item for item in song.arrangements if normalize_text(item.name) == normalize_text(name)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConflictError("Arrangement reconciliation is ambiguous", song_id=song_id)
        try:
            response = await self._write("POST", f"songs/{song_id}/arrangements", {"name": name, "isDefault": True})
            return _parse_arrangement(expect_mapping(response, "root").get("data"))
        except (IndeterminateWriteError, SchemaDriftError):
            song = await self.get_song(song_id)
            matches = [item for item in song.arrangements if normalize_text(item.name) == normalize_text(name)]
            if len(matches) == 1:
                return matches[0]
            raise

    async def insert_agenda_song(
        self,
        event_id: str,
        arrangement_id: str,
        defaults: Mapping[str, Any],
        action_id: str,
        *,
        before_item_id: str | None = None,
        after_item_id: str | None = None,
    ) -> AgendaItem:
        if before_item_id and after_item_id:
            raise ValueError("before_item_id and after_item_id are mutually exclusive")
        before = await self.get_agenda(event_id)
        params: dict[str, str] = {}
        if before_item_id:
            params["before_id"] = before_item_id
        if after_item_id:
            params["after_id"] = after_item_id
        try:
            response = await self._write(
                "POST", f"events/{event_id}/agenda/items", _agenda_payload(arrangement_id, defaults), params=params
            )
            return _parse_written_agenda_song(
                expect_mapping(response, "root").get("data"),
                arrangement_id=arrangement_id,
            )
        except (IndeterminateWriteError, SchemaDriftError):
            after = await self.get_agenda(event_id)
            old_ids = {item.id for item in before.items}
            candidates = [
                item for item in after.items if item.id not in old_ids and item.type == "song" and item.arrangement_id == arrangement_id
            ]
            if len(candidates) == 1 and _insert_position_matches(
                before,
                after,
                candidates[0],
                before_item_id=before_item_id,
                after_item_id=after_item_id,
            ):
                return candidates[0]
            raise

    async def replace_agenda_song(
        self,
        event_id: str,
        item_id: str,
        arrangement_id: str,
        defaults: Mapping[str, Any],
        action_id: str,
    ) -> AgendaItem:
        try:
            response = await self._write(
                "PUT", f"events/{event_id}/agenda/items/{item_id}", _agenda_payload(arrangement_id, defaults)
            )
            return _parse_written_agenda_song(
                expect_mapping(response, "root").get("data"),
                arrangement_id=arrangement_id,
                expected_item_id=item_id,
            )
        except (IndeterminateWriteError, SchemaDriftError):
            agenda = await self.get_agenda(event_id)
            item = next((value for value in agenda.items if value.id == item_id), None)
            if item and item.type == "song" and item.arrangement_id == arrangement_id:
                return item
            raise

    async def delete_agenda_item(self, event_id: str, item_id: str, action_id: str) -> None:
        try:
            await self._write("DELETE", f"events/{event_id}/agenda/items/{item_id}", None)
        except (IndeterminateWriteError, SchemaDriftError):
            agenda = await self.get_agenda(event_id)
            if all(item.id != item_id for item in agenda.items):
                return
            raise

    async def _find_reconciled_song(self, payload: Mapping[str, Any]) -> TargetSong | None:
        probe = SourceSong(
            id="reconcile",
            name=str(payload["name"]),
            artist=str(payload.get("author") or ""),
            ccli=str(payload["ccli"]) if payload.get("ccli") else None,
        )
        songs = tuple(await self.list_songs())
        matched = match_song(probe, songs)
        if matched.ambiguous:
            raise ConflictError("Song reconciliation is ambiguous")
        if matched.target is None:
            return None
        arrangement_name = normalize_text(str(payload["arrangement_name"]))
        if not any(normalize_text(item.name) == arrangement_name for item in matched.target.arrangements):
            raise SchemaDriftError(
                "Reconciled ChurchTools song exists without the required atomic arrangement",
                song_id=matched.target.id,
            )
        return matched.target

    async def _ensure_validated(self) -> None:
        if self._validated:
            return
        async with self._validation_lock:
            if not self._validated:
                await self.validate()

    async def _get(self, endpoint: str, params: Mapping[str, Any] | None = None, *, ensure_auth: bool = True) -> Any:
        if ensure_auth:
            await self._ensure_validated()
        return await request_json(
            self._client,
            "GET",
            f"{self.base_url}/api/{endpoint}",
            headers=self._headers,
            params=params,
            policy=self._policy,
            sleep=self._sleep,
        )

    async def _write(
        self,
        method: str,
        endpoint: str,
        body: Mapping[str, Any] | None,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        await self._ensure_validated()
        return await request_json(
            self._client,
            method,
            f"{self.base_url}/api/{endpoint}",
            headers={**self._headers, "Content-Type": "application/json"},
            params=params,
            json=body,
            write=True,
            policy=self._policy,
            sleep=self._sleep,
        )

    async def _get_all(
        self,
        endpoint: str,
        params: Mapping[str, Any],
        *,
        allow_missing_pagination: bool = False,
    ) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        page = 1
        last_page = 1
        seen_pages: set[str] = set()
        aggregate_bytes = 0
        requested_limit = params.get("limit")
        if (
            not isinstance(requested_limit, int)
            or isinstance(requested_limit, bool)
            or requested_limit < 1
        ):
            raise SchemaDriftError(
                "ChurchTools pagination requires a positive limit",
                endpoint=endpoint,
            )
        while page <= last_page:
            if page > _MAX_PAGES:
                raise SchemaDriftError("ChurchTools pagination exceeded safety limit", endpoint=endpoint)
            payload = expect_mapping(await self._get(endpoint, {**params, "page": page}), "root")
            values = expect_list(payload.get("data"), "data")
            if len(values) > requested_limit:
                raise SchemaDriftError(
                    "ChurchTools page exceeded the requested limit",
                    endpoint=endpoint,
                    page=page,
                )
            if len(result) + len(values) > _MAX_SNAPSHOT_RECORDS:
                raise SchemaDriftError(
                    "ChurchTools snapshot exceeded the record limit",
                    endpoint=endpoint,
                )
            canonical_page = json.dumps(
                values, sort_keys=True, separators=(",", ":"), default=str
            )
            aggregate_bytes += len(canonical_page.encode("utf-8"))
            if aggregate_bytes > _MAX_SNAPSHOT_BYTES:
                raise SchemaDriftError(
                    "ChurchTools snapshot exceeded the aggregate size limit",
                    endpoint=endpoint,
                )
            page_signature = hashlib.sha256(
                canonical_page.encode("utf-8")
            ).hexdigest()
            if page_signature in seen_pages:
                raise SchemaDriftError(
                    "ChurchTools pagination repeated a page",
                    endpoint=endpoint,
                    page=page,
                )
            seen_pages.add(page_signature)
            result.extend(expect_mapping(value, "data[]") for value in values)
            raw_meta = payload.get("meta")
            raw_pagination = (
                raw_meta.get("pagination")
                if isinstance(raw_meta, Mapping)
                else None
            )
            if raw_pagination is None and allow_missing_pagination:
                # The documented date-range event representation may omit page
                # metadata because it already contains the complete range. Do
                # not guess another page from an exactly-full response: servers
                # that ignore `page` would otherwise repeat it indefinitely.
                break
            meta = expect_mapping(raw_meta, "meta")
            pagination = expect_mapping(raw_pagination, "meta.pagination")
            raw_last = pagination.get("lastPage")
            if not isinstance(raw_last, int) or isinstance(raw_last, bool) or raw_last < page:
                raise SchemaDriftError("ChurchTools pagination did not advance", endpoint=endpoint, page=page)
            if raw_last > _MAX_PAGES:
                raise SchemaDriftError(
                    "ChurchTools pagination exceeded safety limit",
                    endpoint=endpoint,
                    last_page=raw_last,
                )
            last_page = raw_last
            page += 1
        return result


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".church.tools")
        or hostname == "church.tools"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.port not in (None, 443))
    ):
        raise ValueError("ChurchTools URL must be an HTTPS *.church.tools origin")
    if parsed.path not in ("", "/"):
        raise ValueError("ChurchTools URL must not contain a path")
    return f"https://{hostname}"


def _parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise SchemaDriftError(f"ChurchTools field '{label}' is not a datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaDriftError(f"ChurchTools field '{label}' is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SchemaDriftError(f"ChurchTools field '{label}' lacks timezone information")
    return parsed


def _parse_event(
    value: Any, calendar_campuses: Mapping[str, str]
) -> TargetEvent:
    item = expect_mapping(value, "event")
    calendar = expect_mapping(item.get("calendar") or {}, "event.calendar")
    attributes = expect_mapping(calendar.get("domainAttributes") or {}, "calendar.domainAttributes")
    raw_calendar_id = calendar.get("domainIdentifier")
    calendar_id = str(raw_calendar_id) if raw_calendar_id is not None else None
    return TargetEvent(
        id=str(item["id"]),
        name=str(item.get("name") or ""),
        starts_at=_parse_datetime(item.get("startDate"), "event.startDate"),
        campus_name=str(attributes["campusName"]) if attributes.get("campusName") is not None else None,
        # Event responses expose only campusName. The stable campus ID is
        # derived from the documented calendar.campusId relation so it matches
        # the IDs offered by the metadata endpoint.
        campus_id=calendar_campuses.get(calendar_id) if calendar_id else None,
        calendar_id=calendar_id,
    )


def _parse_arrangement(value: Any) -> Arrangement:
    item = expect_mapping(value, "arrangement")
    if item.get("id") is None or not isinstance(item.get("name"), str):
        raise SchemaDriftError("ChurchTools arrangement lacks id or name")
    return Arrangement(str(item["id"]), item["name"], bool(item.get("isDefault")))


def _parse_song(value: Any, *, require_arrangements: bool = False) -> TargetSong:
    item = expect_mapping(value, "song")
    if item.get("id") is None or not isinstance(item.get("name"), str):
        raise SchemaDriftError("ChurchTools song lacks id or name")
    if require_arrangements and "arrangements" not in item:
        raise SchemaDriftError("ChurchTools song list omitted requested arrangements")
    arrangements = tuple(_parse_arrangement(value) for value in expect_list(item.get("arrangements") or [], "arrangements"))
    return TargetSong(
        id=str(item["id"]),
        name=item["name"],
        author=str(item.get("author") or ""),
        ccli=str(item["ccli"]) if item.get("ccli") is not None else None,
        arrangements=arrangements,
    )


def _parse_agenda_item(value: Any, fallback_position: int) -> AgendaItem:
    item = expect_mapping(value, "agenda item")
    if item.get("id") is None or not isinstance(item.get("type"), str):
        raise SchemaDriftError("ChurchTools agenda item lacks id or type")
    song = expect_mapping(item.get("song") or {}, "agenda item.song")
    raw_position = item.get("position", fallback_position)
    if not isinstance(raw_position, int) or isinstance(raw_position, bool):
        raise SchemaDriftError("ChurchTools agenda item position is not an integer")
    return AgendaItem(
        id=str(item["id"]),
        position=raw_position,
        type=item["type"],
        title=str(item["title"]) if item.get("title") is not None else None,
        song_id=str(song["songId"]) if song.get("songId") is not None else None,
        arrangement_id=(
            str(song["arrangementId"])
            if song.get("arrangementId") is not None
            else (str(item["arrangementId"]) if item.get("arrangementId") is not None else None)
        ),
    )


def _parse_written_agenda_song(
    value: Any,
    *,
    arrangement_id: str,
    expected_item_id: str | None = None,
) -> AgendaItem:
    """Validate the semantic result of an agenda write.

    A structurally valid 2xx body without the expected song identity is just
    as indeterminate as an empty or malformed body: the server may have
    committed while a proxy returned a stale/incomplete representation.
    """

    item = _parse_agenda_item(value, 0)
    if (
        item.type != "song"
        or item.song_id is None
        or item.arrangement_id != arrangement_id
        or (expected_item_id is not None and item.id != expected_item_id)
    ):
        raise SchemaDriftError(
            "ChurchTools agenda write response lacks the expected song identity"
        )
    return item


def _insert_position_matches(
    before: Agenda,
    after: Agenda,
    candidate: AgendaItem,
    *,
    before_item_id: str | None,
    after_item_id: str | None,
) -> bool:
    """Require positional evidence before adopting an indeterminate insert.

    A matching arrangement anywhere in the agenda is not proof that our write
    committed: a user may have inserted the same song concurrently. The newly
    observed item must occupy the exact requested neighbour position.
    """

    before_items = tuple(
        sorted(before.items, key=lambda item: (item.position, item.id))
    )
    after_items = tuple(
        sorted(after.items, key=lambda item: (item.position, item.id))
    )
    if before_item_id is not None:
        if all(item.id != before_item_id for item in before_items):
            return False
        anchor_index = next(
            (
                index
                for index, item in enumerate(after_items)
                if item.id == before_item_id
            ),
            -1,
        )
        return anchor_index > 0 and after_items[anchor_index - 1].id == candidate.id
    if after_item_id is not None:
        if all(item.id != after_item_id for item in before_items):
            return False
        anchor_index = next(
            (
                index
                for index, item in enumerate(after_items)
                if item.id == after_item_id
            ),
            -1,
        )
        return (
            0 <= anchor_index < len(after_items) - 1
            and after_items[anchor_index + 1].id == candidate.id
        )
    return (
        not before_items
        and len(after_items) == 1
        and after_items[0].id == candidate.id
    )


def _agenda_payload(arrangement_id: str, defaults: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "song",
        "title": "",
        "responsible": "",
        "duration": 0,
        "arrangementId": _positive_integer_id(
            arrangement_id, "ChurchTools arrangement ID"
        ),
    }
    for key in ("title", "note", "responsible", "duration"):
        if key in defaults:
            payload[key] = defaults[key]
    return payload


def _named_metadata(value: Any, label: str) -> dict[str, str]:
    item = expect_mapping(value, label)
    if item.get("id") is None:
        raise SchemaDriftError(f"ChurchTools {label} lacks id")
    identifier = str(item["id"])
    return {"id": identifier, "name": str(item.get("name") or identifier)}


def _calendar_campus_ids(payload: Any) -> dict[str, str]:
    root = expect_mapping(payload, "calendars root")
    result: dict[str, str] = {}
    for raw in expect_list(root.get("data"), "calendars data"):
        calendar = expect_mapping(raw, "calendar")
        if calendar.get("id") is None or calendar.get("campusId") is None:
            continue
        result[str(calendar["id"])] = str(calendar["campusId"])
    return result


def _positive_integer_id(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{label} must be a positive integer")
    return parsed
