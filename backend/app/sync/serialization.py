"""Strict reconstruction of persisted plan JSON for crash recovery."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .errors import SchemaDriftError
from .models import (
    ActionKind,
    EventPlan,
    EventPlanStatus,
    IssueSeverity,
    PlanIssue,
    PlannedAction,
    SyncPlan,
)


def sync_plan_from_dict(value: Mapping[str, Any]) -> SyncPlan:
    try:
        preparation = tuple(_action(item) for item in _list(value, "preparation_actions"))
        events = tuple(_event(item) for item in _list(value, "events"))
        created_at = datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError("created_at is naive")
        return SyncPlan(
            run_id=str(value["run_id"]),
            profile_id=str(value["profile_id"]),
            profile_revision=int(value["profile_revision"]),
            created_at=created_at,
            preparation_actions=preparation,
            events=events,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaDriftError("Persisted sync plan has an invalid schema") from exc


def _event(value: Any) -> EventPlan:
    item = _mapping(value, "event")
    return EventPlan(
        id=str(item["id"]),
        source_event_id=str(item["source_event_id"]),
        target_event_id=str(item["target_event_id"]) if item.get("target_event_id") is not None else None,
        status=EventPlanStatus(str(item["status"])),
        initial_agenda_fingerprint=(
            str(item["initial_agenda_fingerprint"])
            if item.get("initial_agenda_fingerprint") is not None
            else None
        ),
        actions=tuple(_action(action) for action in _list(item, "actions")),
        issues=tuple(_issue(issue) for issue in _list(item, "issues")),
    )


def _action(value: Any) -> PlannedAction:
    item = _mapping(value, "action")
    payload = _mapping(item.get("payload"), "action.payload")
    dependencies = _list(item, "dependencies")
    return PlannedAction(
        id=str(item["id"]),
        ordinal=int(item["ordinal"]),
        kind=ActionKind(str(item["kind"])),
        payload=dict(payload),
        event_plan_id=str(item["event_plan_id"]) if item.get("event_plan_id") is not None else None,
        dependencies=tuple(str(dependency) for dependency in dependencies),
    )


def _issue(value: Any) -> PlanIssue:
    item = _mapping(value, "issue")
    return PlanIssue(
        code=str(item["code"]),
        message=str(item["message"]),
        severity=IssueSeverity(str(item.get("severity", IssueSeverity.ERROR.value))),
        details=dict(_mapping(item.get("details") or {}, "issue.details")),
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaDriftError(f"Persisted plan field '{label}' is not an object")
    return value


def _list(value: Mapping[str, Any], key: str) -> list[Any]:
    item = value.get(key)
    if not isinstance(item, list):
        raise SchemaDriftError(f"Persisted plan field '{key}' is not a list")
    return item
