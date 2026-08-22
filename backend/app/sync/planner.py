"""Deterministic plan construction; this module performs no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .matching import EventMatch, SongIndex, match_events, normalize_text
from .models import (
    ActionKind,
    Agenda,
    AgendaAnchor,
    AgendaItem,
    EventPlan,
    EventPlanStatus,
    IssueSeverity,
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
    ) -> SyncPlan:
        matches = match_events(profile, tuple(source_events), tuple(target_events))
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
                )
            )
        needed_resources = {
            dependency
            for event in event_plans
            if event.status is EventPlanStatus.READY
            for action in event.actions
            for dependency in action.dependencies
        }
        preparation = [
            action
            for action in preparation
            if needed_resources.intersection(_produced_resource_keys(action))
        ]
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
    ) -> tuple[dict[str, _SongResource], dict[str, PlanIssue], list[PlannedAction]]:
        source_candidates: dict[str, list[SourceSong]] = {}
        for song in source_songs:
            source_candidates.setdefault(song.id, []).append(song)

        required_ids = sorted(
            {
                song_id
                for match in matches
                if match.status is EventPlanStatus.READY
                for song_id in match.source.song_ids
            }
        )
        resources: dict[str, _SongResource] = {}
        issues: dict[str, PlanIssue] = {}
        actions: list[PlannedAction] = []
        target_index = SongIndex(tuple(target_songs))
        for source_id in required_ids:
            candidates = source_candidates.get(source_id, [])
            if not candidates:
                issues[source_id] = PlanIssue(
                    "source_song_missing",
                    "Ein im Service verwendeter WorshipTools-Song fehlt im Songkatalog",
                    details={"source_song_id": source_id},
                )
                continue
            if len(candidates) > 1:
                issues[source_id] = PlanIssue(
                    "source_song_ambiguous",
                    "Die WorshipTools-Song-ID ist im Katalog nicht eindeutig",
                    details={"source_song_id": source_id},
                )
                continue
            source = candidates[0]
            matched = target_index.match(source)
            if matched.ambiguous:
                issues[source_id] = PlanIssue(
                    "target_song_ambiguous",
                    "Der WorshipTools-Song passt auf mehrere ChurchTools-Songs",
                    details={"source_song_id": source_id, "strategy": matched.strategy},
                )
                continue
            if matched.target:
                arrangement = _select_arrangement(matched.target)
                if arrangement:
                    resources[source_id] = _SongResource(
                        source, matched.target.id, None, arrangement.id, None
                    )
                    continue
                arrangement_key = f"arrangement:{source_id}"
                actions.append(
                    factory.make(
                        ActionKind.CREATE_ARRANGEMENT,
                        source_id,
                        {
                            "source_song_id": source_id,
                            "target_song_id": matched.target.id,
                            "name": profile.arrangement_name,
                            "resource_key": arrangement_key,
                        },
                    )
                )
                resources[source_id] = _SongResource(
                    source, matched.target.id, None, None, arrangement_key
                )
                continue
            if not profile.auto_create_songs:
                issues[source_id] = PlanIssue(
                    "target_song_missing",
                    "Kein eindeutiger ChurchTools-Song gefunden; automatisches Anlegen ist deaktiviert",
                    details={"source_song_id": source_id},
                )
                continue

            song_key = f"song:{source_id}"
            arrangement_key = f"arrangement:{source_id}"
            actions.append(
                factory.make(
                    ActionKind.CREATE_SONG,
                    source_id,
                    {
                        "source_song_id": source_id,
                        "name": source.name,
                        "author": source.artist,
                        "ccli": source.ccli,
                        "category_id": profile.song_category_id,
                        "song_resource_key": song_key,
                        "arrangement_name": profile.arrangement_name,
                        "arrangement_resource_key": arrangement_key,
                    },
                )
            )
            resources[source_id] = _SongResource(source, None, song_key, None, arrangement_key)

        # A preparation write is allowed only when at least one otherwise
        # plannable event depends on that song.  This guarantees that an event
        # with one ambiguous song causes no incidental song creation at all.
        viable_song_ids = {
            song_id
            for match in matches
            if match.status is EventPlanStatus.READY
            and not any(song_id in issues for song_id in match.source.song_ids)
            for song_id in match.source.song_ids
        }
        actions = [action for action in actions if action.payload.get("source_song_id") in viable_song_ids]
        return resources, issues, actions

    def _plan_event(
        self,
        run_id: str,
        profile: ProfileConfig,
        match: EventMatch,
        resources: Mapping[str, _SongResource],
        song_issues: Mapping[str, PlanIssue],
        agendas: Mapping[str, Agenda],
        ownerships: Mapping[str, Sequence[Ownership]],
        factory: _ActionFactory,
    ) -> EventPlan:
        event_plan_id = stable_action_id(run_id, "event", match.source.id)
        if match.status is not EventPlanStatus.READY or match.target is None:
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=None,
                status=match.status,
                issues=match.issues,
            )
        event_issues = tuple(
            song_issues[song_id] for song_id in dict.fromkeys(match.source.song_ids) if song_id in song_issues
        )
        if event_issues:
            ambiguous = any(issue.code.endswith("ambiguous") for issue in event_issues)
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=match.target.id,
                status=EventPlanStatus.AMBIGUOUS if ambiguous else EventPlanStatus.FAILED,
                issues=event_issues,
            )
        agenda = agendas.get(match.target.id)
        if agenda is None:
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=match.target.id,
                status=EventPlanStatus.FAILED,
                issues=(PlanIssue("agenda_missing", "Für das ChurchTools-Event wurde keine Agenda gefunden"),),
            )
        if not profile.placements:
            return EventPlan(
                id=event_plan_id,
                source_event_id=match.source.id,
                target_event_id=match.target.id,
                status=EventPlanStatus.FAILED,
                initial_agenda_fingerprint=agenda.fingerprint,
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
            initial_agenda_fingerprint=agenda.fingerprint,
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
        resources: Mapping[str, _SongResource],
        event_plan_id: str,
        factory: _ActionFactory,
    ) -> tuple[list[PlannedAction], list[PlanIssue]]:
        items = tuple(sorted(agenda.items, key=lambda item: (item.position, item.id)))
        actions: list[PlannedAction] = []
        issues: list[PlanIssue] = []
        used_source_indexes: set[int] = set()
        reserved_item_ids: set[str] = set()
        desired_keys: set[str] = set()
        ownership_by_item = {ownership.agenda_item_id: ownership for ownership in ownerships}

        for placement in profile.placements:
            indexes = tuple(range(len(source_event.song_ids)))[placement.song_start : placement.song_end]
            overlap = used_source_indexes.intersection(indexes)
            if overlap:
                issues.append(
                    PlanIssue(
                        "placement_overlap",
                        "Song-Platzierungen überschneiden sich",
                        details={"placement_id": placement.id, "song_indexes": sorted(overlap)},
                    )
                )
                continue
            used_source_indexes.update(indexes)
            anchors = [item for item in items if _anchor_matches(placement.anchor, item)]
            if len(anchors) != 1:
                issues.append(
                    PlanIssue(
                        "anchor_not_unique",
                        "Der Agenda-Anker wurde nicht eindeutig gefunden",
                        details={"placement_id": placement.id, "matches": [item.id for item in anchors]},
                    )
                )
                continue
            anchor_index = items.index(anchors[0])
            if placement.relation.value == "before":
                cursor = anchor_index
            elif placement.relation.value == "after":
                cursor = anchor_index + 1
            else:
                cursor = anchor_index

            last_item_id: str | None = items[cursor - 1].id if cursor > 0 else None
            last_item_key: str | None = None
            for source_index in indexes:
                song_id = source_event.song_ids[source_index]
                resource = resources[song_id]
                source_key = f"{placement.id}:{source_index}:{song_id}"
                desired_keys.add(source_key)
                current = items[cursor] if cursor < len(items) else None
                while current is not None and current.id in reserved_item_ids:
                    cursor += 1
                    current = items[cursor] if cursor < len(items) else None

                base_payload = {
                    "profile_id": profile.id,
                    "target_event_id": target_event_id,
                    "source_key": source_key,
                    "placement_id": placement.id,
                    "defaults": dict(profile.agenda_item_defaults),
                    **resource.payload(),
                }
                if current is not None and current.type == "song":
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


def _select_arrangement(song: TargetSong):
    if not song.arrangements:
        return None
    return next((arrangement for arrangement in song.arrangements if arrangement.is_default), None) or sorted(
        song.arrangements, key=lambda arrangement: arrangement.id
    )[0]


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
