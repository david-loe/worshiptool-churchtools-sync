"""Pure matching functions with explicit ambiguity reporting."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .models import (
    EventPlanStatus,
    EventSelector,
    MatchMode,
    PlanIssue,
    ProfileConfig,
    SourceEvent,
    SourceSong,
    TargetEvent,
    TargetSong,
)

_MAX_REGEX_LENGTH = 256
_REGEX_TIMEOUT_SECONDS = 0.02

try:  # The third-party engine is required for enforceable match timeouts.
    import regex as _safe_regex
except ImportError:  # pragma: no cover - exercised only by incomplete deployments
    _safe_regex = None


class _RegexTimedOut(Exception):
    pass


@dataclass(frozen=True, slots=True)
class EventMatch:
    source: SourceEvent
    target: TargetEvent | None
    status: EventPlanStatus
    issues: tuple[PlanIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class SongMatch:
    target: TargetSong | None
    ambiguous: bool = False
    strategy: str | None = None


class SongIndex:
    """Normalized O(1) lookup while preserving ambiguity information."""

    def __init__(self, songs: tuple[TargetSong, ...] | list[TargetSong]) -> None:
        by_ccli: dict[str, list[TargetSong]] = {}
        by_name_author: dict[tuple[str, str], list[TargetSong]] = {}
        for song in songs:
            ccli = normalize_ccli(song.ccli)
            if ccli:
                by_ccli.setdefault(ccli, []).append(song)
            key = (normalize_text(song.name), normalize_text(song.author))
            by_name_author.setdefault(key, []).append(song)
        self._by_ccli = {key: tuple(values) for key, values in by_ccli.items()}
        self._by_name_author = {
            key: tuple(values) for key, values in by_name_author.items()
        }

    def match(self, source: SourceSong) -> SongMatch:
        ccli = normalize_ccli(source.ccli)
        if ccli:
            matches = self._by_ccli.get(ccli, ())
            if len(matches) == 1:
                return SongMatch(matches[0], strategy="ccli")
            if len(matches) > 1:
                return SongMatch(None, ambiguous=True, strategy="ccli")
        matches = self._by_name_author.get(
            (normalize_text(source.name), normalize_text(source.artist)), ()
        )
        if ccli:
            # Name/author is a useful fallback when ChurchTools has no CCLI,
            # but a different non-empty CCLI is positive evidence that this is
            # another song and must never be silently reused.
            matches = tuple(
                target
                for target in matches
                if not normalize_ccli(target.ccli)
                or normalize_ccli(target.ccli) == ccli
            )
        if len(matches) == 1:
            return SongMatch(matches[0], strategy="name_author")
        if len(matches) > 1:
            return SongMatch(None, ambiguous=True, strategy="name_author")
        return SongMatch(None)


def match_events(
    profile: ProfileConfig,
    source_events: list[SourceEvent] | tuple[SourceEvent, ...],
    target_events: list[TargetEvent] | tuple[TargetEvent, ...],
) -> tuple[EventMatch, ...]:
    selector_errors = tuple(
        error for selector in profile.selectors if (error := _validate_selector(selector)) is not None
    )
    if selector_errors:
        return tuple(
            EventMatch(event, None, EventPlanStatus.FAILED, selector_errors)
            for event in sorted(source_events, key=_source_sort_key)
            if event.song_ids
        )

    result: list[EventMatch] = []
    target_index = _index_target_events(profile, target_events)
    for source in sorted(source_events, key=_source_sort_key):
        if not source.song_ids:
            continue
        try:
            candidates = [
                target
                for target in _candidate_events(profile, source, target_index)
                if _event_time_matches(profile, source, target)
                and (
                    not profile.selectors
                    or any(selector_matches(selector, target) for selector in profile.selectors)
                )
            ]
        except _RegexTimedOut:
            result.append(
                EventMatch(
                    source,
                    None,
                    EventPlanStatus.FAILED,
                    (PlanIssue("regex_timeout", "Der Event-Reguläre-Ausdruck überschritt das Zeitlimit"),),
                )
            )
            continue
        candidates.sort(key=lambda event: (event.starts_at, event.id))
        if not candidates:
            result.append(
                EventMatch(
                    source,
                    None,
                    EventPlanStatus.SKIPPED,
                    (PlanIssue("event_not_found", "Kein passendes ChurchTools-Event gefunden"),),
                )
            )
        elif len(candidates) > 1:
            result.append(
                EventMatch(
                    source,
                    None,
                    EventPlanStatus.AMBIGUOUS,
                    (
                        PlanIssue(
                            "event_ambiguous",
                            "Mehrere ChurchTools-Events passen zum WorshipTools-Service",
                            details={"candidate_ids": [candidate.id for candidate in candidates]},
                        ),
                    ),
                )
            )
        else:
            result.append(EventMatch(source, candidates[0], EventPlanStatus.READY))

    # A target may never be written by two source services in the same run.
    target_counts: dict[str, int] = {}
    for match in result:
        if match.target:
            target_counts[match.target.id] = target_counts.get(match.target.id, 0) + 1
    final: list[EventMatch] = []
    for match in result:
        if match.target and target_counts[match.target.id] > 1:
            final.append(
                EventMatch(
                    match.source,
                    None,
                    EventPlanStatus.AMBIGUOUS,
                    (
                        PlanIssue(
                            "target_event_reused",
                            "Mehrere WorshipTools-Services würden dasselbe ChurchTools-Event verändern",
                            details={"target_event_id": match.target.id},
                        ),
                    ),
                )
            )
        else:
            final.append(match)
    return tuple(final)


def selector_matches(selector: EventSelector, event: TargetEvent) -> bool:
    if selector.campus_ids and event.campus_id not in selector.campus_ids:
        return False
    if selector.calendar_ids and event.calendar_id not in selector.calendar_ids:
        return False
    if selector.name_regex:
        assert _safe_regex is not None
        try:
            if _safe_regex.search(selector.name_regex, event.name, timeout=_REGEX_TIMEOUT_SECONDS) is None:
                return False
        except TimeoutError as exc:
            raise _RegexTimedOut from exc
    if selector.name_contains and normalize_text(selector.name_contains) not in normalize_text(event.name):
        return False
    return True


def match_song(source: SourceSong, target_songs: tuple[TargetSong, ...] | list[TargetSong]) -> SongMatch:
    return SongIndex(target_songs).match(source)


def normalize_ccli(value: str | None) -> str:
    if not value:
        return ""
    return "".join(character for character in unicodedata.normalize("NFKC", value).upper() if character.isalnum())


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(character if character.isalnum() else " " for character in normalized).split())


def _event_time_matches(profile: ProfileConfig, source: SourceEvent, target: TargetEvent) -> bool:
    if profile.match_mode is MatchMode.EXACT_TIME:
        target_utc = target.starts_at.astimezone(timezone.utc)
        return any(start.astimezone(timezone.utc) == target_utc for start in source.starts_at)
    source_zone = ZoneInfo(profile.source_timezone)
    target_zone = ZoneInfo(profile.target_timezone)
    target_date = target.starts_at.astimezone(target_zone).date()
    return any(start.astimezone(source_zone).date() == target_date for start in source.starts_at)


def _index_target_events(
    profile: ProfileConfig, target_events: list[TargetEvent] | tuple[TargetEvent, ...]
) -> dict[datetime | date, tuple[TargetEvent, ...]]:
    values: dict[datetime | date, list[TargetEvent]] = {}
    target_zone = ZoneInfo(profile.target_timezone)
    for event in target_events:
        key: datetime | date
        if profile.match_mode is MatchMode.EXACT_TIME:
            key = event.starts_at.astimezone(timezone.utc)
        else:
            key = event.starts_at.astimezone(target_zone).date()
        values.setdefault(key, []).append(event)
    return {key: tuple(events) for key, events in values.items()}


def _candidate_events(
    profile: ProfileConfig,
    source: SourceEvent,
    target_index: dict[datetime | date, tuple[TargetEvent, ...]],
) -> tuple[TargetEvent, ...]:
    source_zone = ZoneInfo(profile.source_timezone)
    candidates: dict[str, TargetEvent] = {}
    for start in source.starts_at:
        key: datetime | date
        if profile.match_mode is MatchMode.EXACT_TIME:
            key = start.astimezone(timezone.utc)
        else:
            key = start.astimezone(source_zone).date()
        for event in target_index.get(key, ()):
            candidates[event.id] = event
    return tuple(candidates.values())


def _validate_selector(selector: EventSelector) -> PlanIssue | None:
    if not selector.name_regex:
        return None
    if _safe_regex is None:
        return PlanIssue(
            "regex_engine_unavailable",
            "Sichere Event-Regulärausdrücke sind auf diesem Server nicht verfügbar",
        )
    if len(selector.name_regex) > _MAX_REGEX_LENGTH:
        return PlanIssue("invalid_regex", "Der Event-Reguläre-Ausdruck ist zu lang")
    try:
        _safe_regex.compile(selector.name_regex)
    except Exception as exc:
        return PlanIssue("invalid_regex", "Der Event-Reguläre-Ausdruck ist ungültig", details={"reason": str(exc)})
    return None


def _source_sort_key(event: SourceEvent) -> tuple[object, str]:
    return (min(event.starts_at), event.id)
