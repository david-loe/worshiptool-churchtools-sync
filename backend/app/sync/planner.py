"""Deterministic plan construction; this module performs no I/O."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import AbstractSet, Any, Mapping, Sequence

from .matching import EventMatch, SongIndex, match_events, normalize_ccli, normalize_text
from .keys import arrangement_name, normalize_key, select_arrangement, effective_song_key
from .models import (
    ActionKind,
    Agenda,
    AgendaAnchor,
    AgendaItem,
    EventPlan,
    EventPlanStatus,
    IssueSeverity,
    MultipleAnchorPolicy,
    Ownership,
    PlanIssue,
    PlannedAction,
    ProfileConfig,
    SourceEvent,
    SourceSong,
    SyncPlan,
    TargetEvent,
    TargetSong,
    stable_action_id,
)


@dataclass(frozen=True, slots=True)
class _SongResource:
    source: SourceSong
    song_id: str | None
    song_key: str | None
    arrangement_id: str | None
    arrangement_key: str | None
    key: str | None = None

    @property
    def dependencies(self) -> tuple[str, ...]:
        values = [value for value in (self.song_key, self.arrangement_key) if value]
        return tuple(values)

    def payload(self) -> dict[str, Any]:
        return {
            "target_song_id": self.song_id,
            "target_song_key": self.song_key,
            "arrangement_id": self.arrangement_id,
            "arrangement_key": self.arrangement_key,
            "key": self.key,
            "song_name": self.source.name,
        }


class _ActionFactory:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.ordinal = 0

    def make(
        self,
        kind: ActionKind,
        identity: str,
        payload: Mapping[str, Any],
        *,
        event_plan_id: str | None = None,
        dependencies: tuple[str, ...] = (),
    ) -> PlannedAction:
        action = PlannedAction(
            id=stable_action_id(self.run_id, kind.value, event_plan_id or "preparation", identity),
            ordinal=self.ordinal,
            kind=kind,
            payload=dict(payload),
            event_plan_id=event_plan_id,
            dependencies=dependencies,
        )
        self.ordinal += 1
        return action


class SyncPlanner:
    """Builds a complete plan which can be stored before the first write."""

    def plan(
        self,
        *,
        run_id: str,
        profile: ProfileConfig,
        created_at: datetime,
        source_events: Sequence[SourceEvent],
        target_events: Sequence[TargetEvent],
        source_songs: Sequence[SourceSong],
        target_songs: Sequence[TargetSong],
        agendas: Mapping[str, Agenda],
        ownerships: Mapping[str, Sequence[Ownership]],
        event_fingerprints: Mapping[tuple[str, str], str] | None = None,
        config_fingerprint: str | None = None,
        unchanged_event_keys: AbstractSet[tuple[str, str]] = frozenset(),
    ) -> SyncPlan:
        event_fingerprints = event_fingerprints or {}
        source_events = _validate_source_keys(source_events, source_songs)
        matches = match_events(profile, tuple(source_events), tuple(target_events))
        matches = tuple(
            EventMatch(
                match.source,
                match.target,
                EventPlanStatus.SKIPPED,
                (
                    PlanIssue(
                        "source_unchanged",
                        "WorshipTools-Songs und Sync-Konfiguration sind unverändert",
                        severity=IssueSeverity.INFO,
                    ),
                ),
            )
            if match.target is not None
            and not match.source.issues
            and (match.source.id, match.target.id) in unchanged_event_keys
            else match
            for match in matches
        )
        factory = _ActionFactory(run_id)
        resources, song_issues, preparation = self._plan_song_resources(
            profile, matches, source_songs, target_songs, factory
        )
        event_plans: list[EventPlan] = []
        for match in matches:
            event_plans.append(
                self._plan_event(
                    run_id,
                    profile,
                    match,
                    resources,
                    song_issues,
                    agendas,
                    ownerships,
                    factory,
                    event_fingerprints,
                    config_fingerprint,
                )
            )
        needed_resources = {
            dependency
            for event in event_plans
            if event.status is EventPlanStatus.READY
            for action in event.actions
            for dependency in action.dependencies
        }
        # Include the entire dependency closure (an arrangement can depend on
        # a song whose inline arrangement is not used by any viable event).
        retained: list[PlannedAction] = []
        for action in reversed(preparation):
            if needed_resources.intersection(_produced_resource_keys(action)):
                retained.append(action)
                needed_resources.update(action.dependencies)
        preparation = list(reversed(retained))
        return SyncPlan(
            run_id=run_id,
            profile_id=profile.id,
            profile_revision=profile.revision,
            created_at=created_at,
            preparation_actions=tuple(preparation),
            events=tuple(event_plans),
        )

    def _plan_song_resources(
        self,
        profile: ProfileConfig,
        matches: tuple[EventMatch, ...],
        source_songs: Sequence[SourceSong],
        target_songs: Sequence[TargetSong],
        factory: _ActionFactory,
    ) -> tuple[dict[tuple[str, str | None], _SongResource], dict[str, PlanIssue], list[PlannedAction]]:
        source_candidates: dict[str, list[SourceSong]] = {}
        for song in source_songs:
            source_candidates.setdefault(song.id, []).append(song)
        required: dict[str, set[str | None]] = {}
        for match in matches:
            if match.status is not EventPlanStatus.READY or match.source.issues:
                continue
            for index, song_id in enumerate(match.source.song_ids):
                candidates = source_candidates.get(song_id, ())
                key = effective_song_key(match.source, index, candidates[0]) if candidates else None
                required.setdefault(song_id, set()).add(key)

        resources: dict[tuple[str, str | None], _SongResource] = {}
        issues: dict[str, PlanIssue] = {}
        actions: list[PlannedAction] = []
        target_index = SongIndex(tuple(target_songs))
        groups: dict[str, list[tuple[SourceSong, TargetSong | None]]] = {}
        for source_id in sorted(required):
            candidates = source_candidates.get(source_id, [])
            if len(candidates) != 1:
                issues[source_id] = PlanIssue(
                    "source_song_ambiguous" if candidates else "source_song_missing",
                    "Die WorshipTools-Song-ID ist im Katalog nicht eindeutig" if candidates else
                    "Ein im Service verwendeter WorshipTools-Song fehlt im Songkatalog",
                    details={"source_song_id": source_id},
                )
                continue
            source = candidates[0]
            matched = target_index.match(source)
            if matched.ambiguous:
                issues[source_id] = PlanIssue(
                    "target_song_ambiguous", "Der WorshipTools-Song passt auf mehrere ChurchTools-Songs",
                    details={"source_song_id": source_id, "strategy": matched.strategy},
                )
                continue
            if not matched.target and not profile.auto_create_songs:
                issues[source_id] = PlanIssue(
                    "target_song_missing",
                    "Kein eindeutiger ChurchTools-Song gefunden; automatisches Anlegen ist deaktiviert",
                    details={"source_song_id": source_id},
                )
                continue
            identity = (
                f"target:{matched.target.id}" if matched.target else
                f"ccli:{normalize_ccli(source.ccli)}" if normalize_ccli(source.ccli) else
                stable_action_id("song", normalize_text(source.name), normalize_text(source.artist))
            )
            groups.setdefault(identity, []).append((source, matched.target))

        for group in groups.values():
            source, target = group[0]
            keys = {key for candidate, _ in group for key in required[candidate.id]}
            song_id = target.id if target else None
            song_resource = None if target else f"song:{source.id}"
            seed_key = None
            seed_resource = None
            if target is None:
                seed_key = next((key for candidate, _ in group if (key := normalize_key(candidate.key))), None)
                seed_key = seed_key or next(iter(sorted(key for key in keys if key)), None)
                seed_resource = f"arrangement:{source.id}" + (f":{seed_key}" if seed_key else "")
                actions.append(factory.make(ActionKind.CREATE_SONG, source.id, {
                    "source_song_id": source.id,
                    "name": source.name, "author": source.artist, "ccli": source.ccli,
                    "category_id": profile.song_category_id,
                    "song_resource_key": song_resource,
                    "arrangement_name": arrangement_name(profile.arrangement_name, seed_key),
                    "arrangement_resource_key": seed_resource,
                    "key": seed_key,
                }))
            for key in sorted(keys, key=lambda value: value or ""):
                if target is None and (key is None or key == seed_key):
                    resource = _SongResource(source, None, song_resource, None, seed_resource, key)
                else:
                    arrangement = select_arrangement(target.arrangements, key) if target else None
                    if arrangement:
                        resource = _SongResource(source, song_id, None, arrangement.id, None, key)
                    else:
                        arrangement_resource = f"arrangement:{source.id}" + (f":{key}" if key else "")
                        actions.append(factory.make(ActionKind.CREATE_ARRANGEMENT, arrangement_resource, {
                            "source_song_id": source.id,
                            "target_song_id": song_id, "target_song_key": song_resource,
                            "name": arrangement_name(profile.arrangement_name, key),
                            "key": key, "resource_key": arrangement_resource,
                        }, dependencies=(song_resource,) if song_resource else ()))
                        resource = _SongResource(source, song_id, song_resource, None, arrangement_resource, key)
                for candidate, _ in group:
                    if key in required[candidate.id]:
                        resources[(candidate.id, key)] = replace(resource, source=candidate)
        return resources, issues, actions

    def _plan_event(
        self,
        run_id: str,
        profile: ProfileConfig,
        match: EventMatch,
        resources: Mapping[tuple[str, str | None], _SongResource],
        song_issues: Mapping[str, PlanIssue],
        agendas: Mapping[str, Agenda],
        ownerships: Mapping[str, Sequence[Ownership]],
        factory: _ActionFactory,
        event_fingerprints: Mapping[tuple[str, str], str],
        config_fingerprint: str | None,
    ) -> EventPlan:
        target_event_id = match.target.id if match.target is not None else None
        event_plan_id = (
            stable_action_id(run_id, "event", match.source.id, "target", target_event_id)
            if target_event_id is not None
            else stable_action_id(run_id, "event", match.source.id, "unmatched")
        )
        source_fingerprint = (
            event_fingerprints.get((match.source.id, target_event_id))
            if target_event_id is not None
            else None
        )
        snapshots = {
            "source_event_name": match.source.name,
            "source_event_starts_at": match.source.starts_at,
            "target_event_name": match.target.name if match.target is not None else None,
            "target_event_starts_at": (
                match.target.starts_at if match.target is not None else None
            ),
        }
        if match.status is not EventPlanStatus.READY or match.target is None:
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=target_event_id,
                status=match.status,
                **snapshots,
                source_fingerprint=source_fingerprint,
                config_fingerprint=config_fingerprint,
                issues=match.issues,
            )
        event_issues = match.source.issues + tuple(
            song_issues[song_id] for song_id in dict.fromkeys(match.source.song_ids) if song_id in song_issues
        )
        if event_issues:
            ambiguous = any(issue.code.endswith("ambiguous") for issue in event_issues)
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=match.target.id,
                status=EventPlanStatus.AMBIGUOUS if ambiguous else EventPlanStatus.FAILED,
                **snapshots,
                source_fingerprint=source_fingerprint,
                config_fingerprint=config_fingerprint,
                issues=event_issues,
            )
        agenda = agendas.get(match.target.id)
        if agenda is None:
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=match.target.id,
                status=EventPlanStatus.SKIPPED,
                **snapshots,
                source_fingerprint=source_fingerprint,
                config_fingerprint=config_fingerprint,
                issues=(
                    PlanIssue(
                        "agenda_missing",
                        "Für das ChurchTools-Event wurde keine Agenda gefunden",
                        severity=IssueSeverity.WARNING,
                    ),
                ),
            )
        if not profile.placements:
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=match.target.id,
                status=EventPlanStatus.FAILED,
                **snapshots,
                initial_agenda_fingerprint=agenda.fingerprint,
                source_fingerprint=source_fingerprint,
                config_fingerprint=config_fingerprint,
                issues=(PlanIssue("placements_missing", "Das Sync-Profil enthält keine Song-Platzierung"),),
            )
        actions, issues = self._plan_placements(
            profile,
            match.source,
            match.target.id,
            agenda,
            tuple(ownerships.get(match.target.id, ())),
            resources,
            event_plan_id,
            factory,
        )
        has_errors = any(issue.severity is IssueSeverity.ERROR for issue in issues)
        return EventPlan(
            id=event_plan_id,
            source_event_id=match.source.id,
            target_event_id=match.target.id,
            status=EventPlanStatus.FAILED if has_errors else EventPlanStatus.READY,
            **snapshots,
            initial_agenda_fingerprint=agenda.fingerprint,
            source_fingerprint=source_fingerprint,
            config_fingerprint=config_fingerprint,
            actions=() if has_errors else tuple(actions),
            issues=tuple(issues),
        )

    def _plan_placements(
        self,
        profile: ProfileConfig,
        source_event: SourceEvent,
        target_event_id: str,
        agenda: Agenda,
        ownerships: tuple[Ownership, ...],
        resources: Mapping[tuple[str, str | None], _SongResource],
        event_plan_id: str,
        factory: _ActionFactory,
    ) -> tuple[list[PlannedAction], list[PlanIssue]]:
        items = tuple(sorted(agenda.items, key=lambda item: (item.position, item.id)))
        actions: list[PlannedAction] = []
        issues: list[PlanIssue] = []
        reserved_item_ids: set[str] = set()
        desired_keys: set[str] = set()
        ownership_by_item = {ownership.agenda_item_id: ownership for ownership in ownerships}

        for placement in profile.placements:
            indexes = tuple(range(len(source_event.song_ids)))[placement.song_start : placement.song_end]
            anchors = [item for item in items if _anchor_matches(placement.anchor, item)]
            if not anchors:
                issues.append(
                    PlanIssue(
                        "anchor_not_found",
                        "Der Agenda-Anker wurde nicht gefunden",
                        details={"placement_id": placement.id, "matches": []},
                    )
                )
                continue
            if len(anchors) > 1 and placement.multiple_anchor_policy is MultipleAnchorPolicy.FAIL:
                issues.append(
                    PlanIssue(
                        "anchor_not_unique",
                        "Der Agenda-Anker wurde nicht eindeutig gefunden",
                        details={
                            "placement_id": placement.id,
                            "matches": [item.id for item in anchors],
                        },
                    )
                )
                continue
            selected_anchor = anchors[0]
            if len(anchors) > 1:
                issues.append(
                    PlanIssue(
                        "anchor_multiple_first_selected",
                        "Mehrere Agenda-Anker gefunden; der erste Treffer wird verwendet",
                        severity=IssueSeverity.WARNING,
                        details={
                            "placement_id": placement.id,
                            "matches": [item.id for item in anchors],
                            "selected_anchor_id": selected_anchor.id,
                        },
                    )
                )
            anchor_index = items.index(selected_anchor)
            leading_inserts = 0
            if placement.relation.value == "before":
                cursor = anchor_index
                while (
                    cursor > 0
                    and anchor_index - cursor < len(indexes)
                    and items[cursor - 1].type == "song"
                ):
                    cursor -= 1
                # Align the source selection with the anchor: missing slots
                # precede existing ones, and the anchor is never replaced.
                leading_inserts = len(indexes) - (anchor_index - cursor)
            elif placement.relation.value == "after":
                cursor = anchor_index + 1
            else:
                cursor = anchor_index
                # AT a non-song inserts immediately before it. On later runs
                # start at this placement's existing block, not at the text
                # again; unrelated songs preceding the anchor stay outside.
                if selected_anchor.type != "song":
                    while cursor > 0 and items[cursor - 1].type == "song":
                        owner = ownership_by_item.get(items[cursor - 1].id)
                        if (
                            owner is None
                            or owner.profile_id != profile.id
                            or owner.placement_id != placement.id
                        ):
                            break
                        cursor -= 1

            last_item_id: str | None = items[cursor - 1].id if cursor > 0 else None
            last_item_key: str | None = None
            for selection_index, source_index in enumerate(indexes):
                song_id = source_event.song_ids[source_index]
                # The effective key belongs to this occurrence, not to the song ID.
                key = normalize_key(source_event.song_keys[source_index]) if source_event.song_keys else None
                resource = resources[(song_id, key)]
                source_key = f"{placement.id}:{source_index}:{song_id}"
                desired_keys.add(source_key)
                current = items[cursor] if cursor < len(items) else None

                base_payload = {
                    "profile_id": profile.id,
                    "target_event_id": target_event_id,
                    "source_key": source_key,
                    "placement_id": placement.id,
                    "defaults": dict(profile.agenda_item_defaults),
                    **resource.payload(),
                }
                if (
                    selection_index >= leading_inserts
                    and current is not None
                    and current.type == "song"
                ):
                    if current.id in reserved_item_ids:
                        issues.append(
                            PlanIssue(
                                "placement_target_overlap",
                                "Mehrere Song-Platzierungen verwenden denselben Ziel-Slot",
                                details={
                                    "placement_id": placement.id,
                                    "agenda_item_id": current.id,
                                },
                            )
                        )
                        break
                    current_ownership = ownership_by_item.get(current.id)
                    if current_ownership and current_ownership.profile_id != profile.id:
                        issues.append(
                            PlanIssue(
                                "agenda_item_owned_by_other_profile",
                                "Der Ziel-Slot wird bereits von einem anderen Sync-Profil verwaltet",
                                details={"agenda_item_id": current.id},
                            )
                        )
                        break
                    reserved_item_ids.add(current.id)
                    payload = {
                        **base_payload,
                        "agenda_item_id": current.id,
                        "already_owned": bool(
                            current_ownership and current_ownership.profile_id == profile.id
                        ),
                    }
                    kind = (
                        ActionKind.NOOP
                        if resource.song_id is not None
                        and resource.arrangement_id is not None
                        and current.song_id == resource.song_id
                        and current.arrangement_id == resource.arrangement_id
                        else ActionKind.REPLACE_ITEM
                    )
                    actions.append(
                        factory.make(
                            kind,
                            source_key,
                            payload,
                            event_plan_id=event_plan_id,
                            dependencies=resource.dependencies,
                        )
                    )
                    last_item_id = current.id
                    last_item_key = None
                    cursor += 1
                    continue

                item_key = f"agenda:{target_event_id}:{source_key}"
                payload = {
                    **base_payload,
                    "resource_key": item_key,
                    # Reconciliation may only adopt an item that appeared after
                    # this durable plan. Otherwise an unrelated agenda edit
                    # could make a pre-existing, adjacent human item look like
                    # our committed insert.
                    "initial_agenda_item_ids": [item.id for item in items],
                }
                if current is not None:
                    payload["before_item_id"] = current.id
                elif last_item_key:
                    payload["after_item_key"] = last_item_key
                elif last_item_id:
                    payload["after_item_id"] = last_item_id
                actions.append(
                    factory.make(
                        ActionKind.INSERT_ITEM,
                        source_key,
                        payload,
                        event_plan_id=event_plan_id,
                        dependencies=resource.dependencies + ((last_item_key,) if last_item_key else ()),
                    )
                )
                last_item_key = item_key
                last_item_id = None

        item_by_id = {item.id: item for item in items}
        for ownership in sorted(ownerships, key=lambda value: (value.placement_id, value.source_key, value.agenda_item_id)):
            if (
                ownership.profile_id != profile.id
                or ownership.source_key in desired_keys
                or ownership.agenda_item_id in reserved_item_ids
            ):
                continue
            item = item_by_id.get(ownership.agenda_item_id)
            expected_song_id = ownership.fingerprint.get("target_song_id")
            expected_arrangement_id = ownership.fingerprint.get("arrangement_id")
            fingerprint_matches = (
                item is not None
                and item.type == "song"
                and expected_song_id is not None
                and expected_arrangement_id is not None
                and item.song_id == str(expected_song_id)
                and item.arrangement_id == str(expected_arrangement_id)
            )
            remotely_changed = item is not None and not fingerprint_matches
            if remotely_changed:
                issues.append(
                    PlanIssue(
                        "owned_agenda_item_changed",
                        "Ein zuvor verwalteter Agenda-Song wurde in ChurchTools geändert; die Bindung wird ohne Löschen aufgegeben",
                        severity=IssueSeverity.WARNING,
                        details={
                            "agenda_item_id": ownership.agenda_item_id,
                            "expected_song_id": expected_song_id,
                            "expected_arrangement_id": expected_arrangement_id,
                            "current_type": item.type,
                            "current_song_id": item.song_id,
                            "current_arrangement_id": item.arrangement_id,
                        },
                    )
                )
            payload = {
                "profile_id": profile.id,
                "target_event_id": target_event_id,
                "agenda_item_id": ownership.agenda_item_id,
                "source_key": ownership.source_key,
                "placement_id": ownership.placement_id,
                # Ownership is delete authority only while the exact song and
                # arrangement last verified by this profile are still present.
                "cleanup_only": not fingerprint_matches,
            }
            actions.append(
                factory.make(
                    ActionKind.DELETE_OWNED_ITEM if fingerprint_matches else ActionKind.NOOP,
                    f"delete:{ownership.agenda_item_id}",
                    payload,
                    event_plan_id=event_plan_id,
                )
            )
        if any(issue.severity is IssueSeverity.ERROR for issue in issues):
            # A malformed placement must cause zero writes for this event.
            return [], issues
        return actions, issues


def _validate_source_keys(
    events: Sequence[SourceEvent], songs: Sequence[SourceSong]
) -> tuple[SourceEvent, ...]:
    catalog = {song.id: song for song in songs}
    result = []
    for event in events:
        keys = event.song_keys or tuple(catalog[song_id].key if song_id in catalog else None for song_id in event.song_ids)
        issues = list(event.issues)
        for index, key in enumerate(keys):
            if key and normalize_key(key) is None:
                issues.append(PlanIssue(
                    "invalid_song_key", "Die WorshipTools-Tonart ist ungültig",
                    details={"source_song_id": event.song_ids[index], "song_index": index, "key": key},
                ))
        result.append(replace(event, song_keys=keys, issues=tuple(issues)))
    return tuple(result)


def _anchor_matches(anchor: AgendaAnchor, item: AgendaItem) -> bool:
    if not any((anchor.item_id, anchor.item_type, anchor.title)):
        return False
    if anchor.item_id is not None and anchor.item_id != item.id:
        return False
    if anchor.item_type is not None and anchor.item_type != item.type:
        return False
    if anchor.title is not None and normalize_text(anchor.title) != normalize_text(item.title or ""):
        return False
    return True


def _produced_resource_keys(action: PlannedAction) -> tuple[str, ...]:
    if action.kind is ActionKind.CREATE_SONG:
        return tuple(
            str(value)
            for value in (
                action.payload.get("song_resource_key"),
                action.payload.get("arrangement_resource_key"),
            )
            if value
        )
    value = action.payload.get("resource_key")
    return (str(value),) if value else ()
