"""Compatibility helpers for persisted event-selector JSON.

New API payloads use only the plural ID arrays.  This module deliberately
keeps the small read-side compatibility shim separate from request validation:
legacy database rows can still be rendered and executed, while deprecated
fields can no longer enter through the API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def canonicalize_persisted_event_rules(value: Any) -> list[dict[str, Any]]:
    """Return the canonical, array-only representation of stored rules.

    ``calendar_id`` used to be accepted alongside ``calendar_ids``.  It is
    promoted only when no canonical calendar list exists, so a stale legacy
    value can never add a hidden filter to a newer rule.  ``campus_name`` is
    intentionally ignored: converting a display name to a stable campus ID
    would require provider metadata and could select the wrong campus.
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []

    result: list[dict[str, Any]] = []
    for raw_rule in value:
        if not isinstance(raw_rule, Mapping):
            continue

        calendar_ids = _canonical_ids(raw_rule.get("calendar_ids"))
        if not calendar_ids:
            legacy_calendar_id = _canonical_id(raw_rule.get("calendar_id"))
            if legacy_calendar_id is not None:
                calendar_ids = [legacy_calendar_id]

        rule: dict[str, Any] = {
            "campus_ids": _canonical_ids(raw_rule.get("campus_ids")),
            "calendar_ids": calendar_ids,
        }
        for field in ("name_contains", "name_regex"):
            field_value = raw_rule.get(field)
            if isinstance(field_value, str) and field_value:
                rule[field] = field_value
        result.append(rule)
    return result


def _canonical_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _canonical_id(item)
        if normalized is not None and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _canonical_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > 100:
        return None
    return normalized
