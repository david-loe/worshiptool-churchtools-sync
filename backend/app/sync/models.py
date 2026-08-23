"""Immutable DTOs used by the sync planner and executor."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


class MatchMode(str, Enum):
    EXACT_TIME = "exact_time"
    DATE_ONLY = "date_only"


class AnchorRelation(str, Enum):
    BEFORE = "before"
    AT = "at"
    AFTER = "after"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"


class EventPlanStatus(str, Enum):
    READY = "ready"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"
    SKIPPED = "skipped"


class ActionKind(str, Enum):
    CREATE_SONG = "create_song"
    CREATE_ARRANGEMENT = "create_arrangement"
    INSERT_ITEM = "insert_item"
    REPLACE_ITEM = "replace_item"
    DELETE_OWNED_ITEM = "delete_owned_item"
    NOOP = "noop"


class ActionStatus(str, Enum):
    PLANNED = "planned"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SourceEvent:
    id: str
    name: str
    starts_at: tuple[datetime, ...]
    song_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("source event id must not be empty")
        if not self.starts_at:
            raise ValueError("source event must contain at least one start time")
        _require_aware(self.starts_at, "source event start")


@dataclass(frozen=True, slots=True)
class TargetEvent:
    id: str
    name: str
    starts_at: datetime
    campus_name: str | None = None
    campus_id: str | None = None
    calendar_id: str | None = None

    def __post_init__(self) -> None:
        _require_aware((self.starts_at,), "target event start")


@dataclass(frozen=True, slots=True)
class SourceSong:
    id: str
    name: str
    artist: str
    ccli: str | None = None


@dataclass(frozen=True, slots=True)
class Arrangement:
    id: str
    name: str
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class TargetSong:
    id: str
    name: str
    author: str
    ccli: str | None = None
    arrangements: tuple[Arrangement, ...] = ()


@dataclass(frozen=True, slots=True)
class AgendaItem:
    id: str
    position: int
    type: str
    title: str | None = None
    song_id: str | None = None
    arrangement_id: str | None = None


@dataclass(frozen=True, slots=True)
class Agenda:
    event_id: str
    items: tuple[AgendaItem, ...]

    @property
    def fingerprint(self) -> str:
        return agenda_fingerprint(self)


@dataclass(frozen=True, slots=True)
class EventSelector:
    name_contains: str | None = None
    name_regex: str | None = None
    campus_ids: tuple[str, ...] = ()
    calendar_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgendaAnchor:
    item_id: str | None = None
    item_type: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class PlacementRule:
    id: str
    anchor: AgendaAnchor
    relation: AnchorRelation = AnchorRelation.AFTER
    song_start: int = 0
    song_end: int | None = None


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    id: str
    revision: int
    source_timezone: str
    target_timezone: str
    match_mode: MatchMode = MatchMode.EXACT_TIME
    selectors: tuple[EventSelector, ...] = ()
    placements: tuple[PlacementRule, ...] = ()
    auto_create_songs: bool = True
    song_category_id: int | None = None
    arrangement_name: str = "Standard-Arrangement"
    agenda_item_defaults: Mapping[str, Any] = field(default_factory=dict)
    lookahead_days: int = 28

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("profile revision must be positive")
        # Fail at profile conversion instead of halfway through a run.
        ZoneInfo(self.source_timezone)
        ZoneInfo(self.target_timezone)
        if not 1 <= self.lookahead_days <= 366:
            raise ValueError("lookahead_days must be between 1 and 366")
        if self.auto_create_songs and self.song_category_id is None:
            raise ValueError("song_category_id is required when automatic song creation is enabled")
        if not self.arrangement_name or len(self.arrangement_name) > 50:
            raise ValueError("arrangement_name must contain between 1 and 50 characters")


@dataclass(frozen=True, slots=True)
class Ownership:
    profile_id: str
    target_event_id: str
    agenda_item_id: str
    source_key: str
    placement_id: str
    fingerprint: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanIssue:
    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.ERROR
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannedAction:
    id: str
    ordinal: int
    kind: ActionKind
    payload: Mapping[str, Any]
    event_plan_id: str | None = None
    dependencies: tuple[str, ...] = ()

    @property
    def mutates_agenda(self) -> bool:
        return self.kind in {
            ActionKind.INSERT_ITEM,
            ActionKind.REPLACE_ITEM,
            ActionKind.DELETE_OWNED_ITEM,
        }


@dataclass(frozen=True, slots=True)
class EventPlan:
    id: str
    source_event_id: str
    target_event_id: str | None
    status: EventPlanStatus
    initial_agenda_fingerprint: str | None = None
    actions: tuple[PlannedAction, ...] = ()
    issues: tuple[PlanIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncPlan:
    run_id: str
    profile_id: str
    profile_revision: int
    created_at: datetime
    preparation_actions: tuple[PlannedAction, ...]
    events: tuple[EventPlan, ...]

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(to_primitive(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(frozen=True, slots=True)
class ActionExecution:
    action_id: str
    status: ActionStatus
    result: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RunSpecification:
    run_id: str
    workspace_id: str
    source_connection_id: str
    target_connection_id: str
    profile: ProfileConfig
    dry_run: bool = False


def agenda_fingerprint(agenda: Agenda) -> str:
    """Hash only fields which influence planning and verification."""

    canonical = [
        {
            "id": item.id,
            "position": item.position,
            "type": item.type,
            "title": item.title,
            "song_id": item.song_id,
            "arrangement_id": item.arrangement_id,
        }
        for item in sorted(agenda.items, key=lambda value: (value.position, value.id))
    ]
    value = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def to_primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    return value


def stable_action_id(run_id: str, *parts: object) -> str:
    raw = "\x1f".join((run_id, *(str(part) for part in parts)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _require_aware(values: Iterable[datetime], label: str) -> None:
    for value in values:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
