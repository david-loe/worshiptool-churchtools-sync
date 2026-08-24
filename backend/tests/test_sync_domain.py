from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

from app.sync.engine import SyncOrchestrator
from app.sync.errors import AuthorizationError
from app.sync.fingerprints import source_event_fingerprint, sync_config_fingerprint
from app.sync.matching import match_events, match_song
from app.sync.models import (
    ActionExecution,
    ActionKind,
    ActionStatus,
    Agenda,
    AgendaAnchor,
    AgendaItem,
    AnchorRelation,
    Arrangement,
    EventPlanStatus,
    EventSyncCheckpoint,
    IssueSeverity,
    MatchMode,
    Ownership,
    PlacementRule,
    ProfileConfig,
    RunSpecification,
    RunStatus,
    SourceEvent,
    SourceSong,
    SyncMode,
    TargetEvent,
    TargetSong,
)
from app.sync.planner import SyncPlanner
from app.sync.serialization import sync_plan_from_dict
from app.sync.testing import (
    FakeSourceProvider,
    FakeTargetProvider,
    MemoryEventLeaseManager,
    MemoryRunRepository,
    StaticProviderRegistry,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def profile(
    *,
    mode: MatchMode = MatchMode.EXACT_TIME,
    sync_mode: SyncMode = SyncMode.SOURCE_CHANGES_ONLY,
    arrangement_name: str = "Standard-Arrangement",
) -> ProfileConfig:
    return ProfileConfig(
        id="profile-1",
        revision=3,
        source_timezone="Europe/Berlin",
        target_timezone="Europe/Berlin",
        sync_mode=sync_mode,
        match_mode=mode,
        placements=(
            PlacementRule(
                "main",
                AgendaAnchor(item_type="header", title="Lobpreis"),
                AnchorRelation.AFTER,
            ),
        ),
        song_category_id=7,
        arrangement_name=arrangement_name,
    )


def test_source_event_fingerprint_tracks_order_and_song_matching_metadata() -> None:
    start = dt("2026-01-01T10:00:00Z")
    songs = (
        SourceSong("one", "  Amazing  Grace ", "JOHN NEWTON", " 123 "),
        SourceSong("two", "Second", "Artist", "456"),
    )
    event = SourceEvent("service", "Service", (start,), ("one", "two"))

    original = source_event_fingerprint(event, songs)

    assert original == source_event_fingerprint(
        event,
        (
            SourceSong("one", "amazing grace", "john newton", "123"),
            songs[1],
        ),
    )
    assert original != source_event_fingerprint(
        SourceEvent("service", "Service", (start,), ("two", "one")), songs
    )
    assert original != source_event_fingerprint(
        SourceEvent("service", "Service", (start,), ("one",)), songs
    )
    assert original != source_event_fingerprint(
        event,
        (SourceSong("one", "Amazing Grace (Live)", "John Newton", "123"), songs[1]),
    )


def test_sync_config_fingerprint_only_tracks_reconciliation_settings() -> None:
    base = profile()
    fingerprint = sync_config_fingerprint(
        base, source_connection_id="wt", target_connection_id="ct"
    )

    assert fingerprint == sync_config_fingerprint(
        replace(
            base,
            revision=99,
            lookahead_days=90,
            sync_mode=SyncMode.ENFORCE_SOURCE,
        ),
        source_connection_id="wt",
        target_connection_id="ct",
    )
    assert fingerprint != sync_config_fingerprint(
        profile(arrangement_name="Live"),
        source_connection_id="wt",
        target_connection_id="ct",
    )
    assert fingerprint != sync_config_fingerprint(
        base, source_connection_id="different-wt", target_connection_id="ct"
    )


def test_plan_fingerprint_remains_compatible_with_pre_snapshot_documents() -> None:
    plan = _plan_split_placements(1)
    legacy_document = plan.as_dict()
    for event in legacy_document["events"]:
        event.pop("source_fingerprint")
        event.pop("config_fingerprint")
    legacy_fingerprint = hashlib.sha256(
        json.dumps(
            legacy_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    loaded = sync_plan_from_dict(legacy_document)

    assert loaded.fingerprint == legacy_fingerprint


def target_song(song_id: str = "ct-song", ccli: str | None = "123") -> TargetSong:
    return TargetSong(
        song_id,
        "Amazing Grace",
        "John Newton",
        ccli,
        (Arrangement(f"arr-{song_id}", "Standard", True),),
    )


def source_song(song_id: str = "wt-song", ccli: str | None = "123") -> SourceSong:
    return SourceSong(song_id, "Amazing Grace", "John Newton", ccli)


def _plan_split_placements(
    song_count: int, placements: tuple[PlacementRule, ...] | None = None
):
    start = dt("2026-01-01T10:00:00Z")
    source_ids = tuple(f"source-{index}" for index in range(song_count))
    source_songs = tuple(
        SourceSong(song_id, f"Song {index}", "Artist", str(100 + index))
        for index, song_id in enumerate(source_ids)
    )
    target_songs = tuple(
        TargetSong(
            f"target-{index}",
            f"Song {index}",
            "Artist",
            str(100 + index),
            (Arrangement(f"arrangement-{index}", "Standard", True),),
        )
        for index in range(song_count)
    )
    configured_placements = placements if placements is not None else (
        PlacementRule(
            "worship",
            AgendaAnchor(item_type="header", title="Lobpreis"),
            AnchorRelation.AFTER,
            song_start=0,
            song_end=-1,
        ),
        PlacementRule(
            "closing",
            AgendaAnchor(item_type="header", title="Abschluss"),
            AnchorRelation.AFTER,
            song_start=-1,
        ),
    )
    split_profile = ProfileConfig(
        id="profile-1",
        revision=3,
        source_timezone="Europe/Berlin",
        target_timezone="Europe/Berlin",
        match_mode=MatchMode.EXACT_TIME,
        placements=configured_placements,
        song_category_id=7,
    )
    return SyncPlanner().plan(
        run_id=f"negative-slices-{song_count}",
        profile=split_profile,
        created_at=dt("2026-01-01T00:00:00Z"),
        source_events=(SourceEvent("source", "Service", (start,), source_ids),),
        target_events=(TargetEvent("target", "Event", start),),
        source_songs=source_songs,
        target_songs=target_songs,
        agendas={
            "target": Agenda(
                "target",
                (
                    AgendaItem("worship-header", 0, "header", "Lobpreis"),
                    AgendaItem("closing-header", 1, "header", "Abschluss"),
                ),
            )
        },
        ownerships={},
    )


def test_negative_song_boundaries_split_off_the_last_song() -> None:
    plan = _plan_split_placements(4)

    assert plan.events[0].status is EventPlanStatus.READY
    assert [action.payload["source_key"] for action in plan.events[0].actions] == [
        "worship:0:source-0",
        "worship:1:source-1",
        "worship:2:source-2",
        "closing:3:source-3",
    ]


def test_negative_song_boundaries_handle_one_or_no_songs() -> None:
    one_song = _plan_split_placements(1)
    no_songs = _plan_split_placements(0)

    assert one_song.events[0].status is EventPlanStatus.READY
    assert [action.payload["source_key"] for action in one_song.events[0].actions] == [
        "closing:0:source-0"
    ]
    assert no_songs.events == ()


def test_overlap_detection_uses_indexes_resolved_from_negative_boundaries() -> None:
    overlapping = _plan_split_placements(
        2,
        (
            PlacementRule(
                "all",
                AgendaAnchor(item_type="header", title="Lobpreis"),
                AnchorRelation.AFTER,
            ),
            PlacementRule(
                "last",
                AgendaAnchor(item_type="header", title="Abschluss"),
                AnchorRelation.AFTER,
                song_start=-1,
            ),
        ),
    )

    assert overlapping.events[0].status is EventPlanStatus.FAILED
    assert overlapping.events[0].issues[0].code == "placement_overlap"
    assert overlapping.events[0].issues[0].details["song_indexes"] == [1]


def test_event_matching_supports_exact_instant_and_local_date() -> None:
    source = SourceEvent("s", "Service", (dt("2026-03-29T10:00:00+02:00"),), ("song",))
    same_instant = TargetEvent("exact", "Gottesdienst", dt("2026-03-29T08:00:00Z"))
    other_time_same_date = TargetEvent("date", "Gottesdienst", dt("2026-03-29T16:00:00Z"))

    exact = match_events(profile(), (source,), (same_instant,))
    by_date = match_events(profile(mode=MatchMode.DATE_ONLY), (source,), (other_time_same_date,))

    assert exact[0].target == same_instant
    assert by_date[0].target == other_time_same_date


def test_event_ambiguity_is_isolated_to_one_source_event() -> None:
    sources = (
        SourceEvent("ambiguous", "A", (dt("2026-01-01T10:00:00Z"),), ("song",)),
        SourceEvent("clear", "B", (dt("2026-01-01T11:00:00Z"),), ("song",)),
    )
    targets = (
        TargetEvent("a1", "A1", dt("2026-01-01T10:00:00Z")),
        TargetEvent("a2", "A2", dt("2026-01-01T10:00:00Z")),
        TargetEvent("b", "B", dt("2026-01-01T11:00:00Z")),
    )

    matches = match_events(profile(), sources, targets)

    assert [match.status for match in matches] == [EventPlanStatus.AMBIGUOUS, EventPlanStatus.READY]
    assert matches[1].target.id == "b"


def test_song_matching_normalizes_ccli_then_falls_back_to_name_author() -> None:
    ccli_match = target_song("by-ccli", "12 3-4")
    name_match = TargetSong("by-name", "  Amazing   Grace ", "JOHN NEWTON", None, ())
    conflicting_ccli = TargetSong(
        "conflict", "Amazing Grace", "John Newton", "different-ccli", ()
    )

    assert match_song(source_song(ccli="1234"), (ccli_match, name_match)).target == ccli_match
    assert match_song(source_song(ccli="999"), (name_match,)).target == name_match
    assert match_song(source_song(ccli="999"), (conflicting_ccli,)).target is None


def test_ambiguous_event_does_not_create_any_of_its_other_missing_songs() -> None:
    source_event = SourceEvent(
        "service", "Service", (dt("2026-01-01T10:00:00Z"),), ("ambiguous", "missing")
    )
    songs = (
        SourceSong("ambiguous", "Duplicate", "Artist", "55"),
        SourceSong("missing", "New Song", "Artist", "66"),
    )
    duplicates = (
        TargetSong("one", "X", "X", "55", (Arrangement("a1", "A", True),)),
        TargetSong("two", "Y", "Y", "55", (Arrangement("a2", "A", True),)),
    )
    target_event = TargetEvent("event", "Event", dt("2026-01-01T10:00:00Z"))
    plan = SyncPlanner().plan(
        run_id="run",
        profile=profile(),
        created_at=dt("2026-01-01T00:00:00Z"),
        source_events=(source_event,),
        target_events=(target_event,),
        source_songs=songs,
        target_songs=duplicates,
        agendas={"event": Agenda("event", (AgendaItem("h", 0, "header", "Lobpreis"),))},
        ownerships={},
    )

    assert plan.events[0].status is EventPlanStatus.AMBIGUOUS
    assert plan.preparation_actions == ()


def test_planner_uses_source_wins_and_deletes_only_owned_items() -> None:
    source_event = SourceEvent("service", "Service", (dt("2026-01-01T10:00:00Z"),), ("new",))
    new_song = TargetSong("new-target", "New", "Artist", "9", (Arrangement("new-arr", "A", True),))
    old_song = TargetSong("old-target", "Old", "Artist", "8", (Arrangement("old-arr", "A", True),))
    target_event = TargetEvent("event", "Event", source_event.starts_at[0])
    agenda = Agenda(
        "event",
        (
            AgendaItem("header", 0, "header", "Lobpreis"),
            AgendaItem("replace", 1, "song", song_id="old-target", arrangement_id="old-arr"),
            AgendaItem("text", 2, "text", "Predigt"),
            AgendaItem("owned-old", 3, "song", song_id="old-target", arrangement_id="old-arr"),
            AgendaItem("foreign", 4, "song", song_id="old-target", arrangement_id="old-arr"),
        ),
    )
    plan = SyncPlanner().plan(
        run_id="run",
        profile=profile(),
        created_at=dt("2026-01-01T00:00:00Z"),
        source_events=(source_event,),
        target_events=(target_event,),
        source_songs=(SourceSong("new", "New", "Artist", "9"),),
        target_songs=(new_song, old_song),
        agendas={"event": agenda},
        ownerships={
            "event": (
                Ownership("profile-1", "event", "replace", "main:99:previous", "main"),
                Ownership(
                    "profile-1",
                    "event",
                    "owned-old",
                    "main:99:old",
                    "main",
                    {
                        "target_song_id": "old-target",
                        "arrangement_id": "old-arr",
                    },
                ),
                Ownership("profile-1", "event", "text", "main:98:text-was-song", "main"),
            )
        },
    )

    actions = plan.events[0].actions
    assert actions[0].kind is ActionKind.REPLACE_ITEM
    assert {action.kind for action in actions[1:]} == {
        ActionKind.DELETE_OWNED_ITEM,
        ActionKind.NOOP,
    }
    assert actions[0].payload["agenda_item_id"] == "replace"
    delete = next(action for action in actions if action.kind is ActionKind.DELETE_OWNED_ITEM)
    cleanup = next(action for action in actions if action.kind is ActionKind.NOOP)
    assert delete.payload["agenda_item_id"] == "owned-old"
    assert cleanup.payload["agenda_item_id"] == "text"
    assert cleanup.payload["cleanup_only"] is True
    assert all(action.payload.get("agenda_item_id") != "foreign" for action in actions)


def test_noop_requires_arrangement_match_and_does_not_claim_foreign_item() -> None:
    start = dt("2026-01-01T10:00:00Z")
    source_event = SourceEvent("service", "Service", (start,), ("song",))
    event = TargetEvent("event", "Event", start)
    song = target_song("target", "123")

    def make_plan(arrangement_id: str):
        return SyncPlanner().plan(
            run_id=f"run-{arrangement_id}",
            profile=profile(),
            created_at=dt("2026-01-01T00:00:00Z"),
            source_events=(source_event,),
            target_events=(event,),
            source_songs=(source_song("song", "123"),),
            target_songs=(song,),
            agendas={
                "event": Agenda(
                    "event",
                    (
                        AgendaItem("header", 0, "header", "Lobpreis"),
                        AgendaItem(
                            "slot",
                            1,
                            "song",
                            song_id="target",
                            arrangement_id=arrangement_id,
                        ),
                    ),
                )
            },
            ownerships={},
        )

    wrong_arrangement = make_plan("other-arrangement")
    identical_foreign = make_plan("arr-target")

    assert wrong_arrangement.events[0].actions[0].kind is ActionKind.REPLACE_ITEM
    noop = identical_foreign.events[0].actions[0]
    assert noop.kind is ActionKind.NOOP
    assert noop.payload["already_owned"] is False


def test_other_profile_ownership_fails_event_and_suppresses_preparation_writes() -> None:
    start = dt("2026-01-01T10:00:00Z")
    source_event = SourceEvent("service", "Service", (start,), ("new",))
    event = TargetEvent("event", "Event", start)
    agenda = Agenda(
        "event",
        (
            AgendaItem("header", 0, "header", "Lobpreis"),
            AgendaItem("slot", 1, "song", song_id="old", arrangement_id="old-arr"),
        ),
    )
    plan = SyncPlanner().plan(
        run_id="collision-run",
        profile=profile(),
        created_at=dt("2026-01-01T00:00:00Z"),
        source_events=(source_event,),
        target_events=(event,),
        source_songs=(SourceSong("new", "Brand New", "Artist", "765"),),
        target_songs=(
            TargetSong("old", "Old", "A", "1", (Arrangement("old-arr", "A", True),)),
        ),
        agendas={"event": agenda},
        ownerships={
            "event": (
                Ownership("other-profile", "event", "slot", "main:0:other", "main"),
            )
        },
    )

    assert plan.events[0].status is EventPlanStatus.FAILED
    assert plan.events[0].actions == ()
    assert plan.events[0].issues[0].code == "agenda_item_owned_by_other_profile"
    assert plan.preparation_actions == ()


def _engine_fixture(*, dry_run: bool = False, fail_events: set[str] | None = None):
    start = dt("2026-01-01T10:00:00Z")
    source_events = (SourceEvent("service", "Service", (start,), ("new",)),)
    target_events = (TargetEvent("event", "Event", start),)
    source_provider = FakeSourceProvider(source_events, (SourceSong("new", "New Song", "Artist", "777"),))
    repository = MemoryRunRepository(
        RunSpecification("run", "workspace", "wt", "ct", profile(), dry_run=dry_run)
    )
    target_provider = FakeTargetProvider(
        target_events,
        (),
        {"event": Agenda("event", (AgendaItem("header", 0, "header", "Lobpreis"),))},
        before_write=lambda kind, action: repository.plan is not None
        or (_ for _ in ()).throw(AssertionError("write before durable plan")),
        fail_event_ids=fail_events,
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source_provider, target_provider),
        clock=type("Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")})(),
        event_leases=MemoryEventLeaseManager(),
    )
    return orchestrator, repository, target_provider


def test_orchestrator_persists_plan_then_atomically_creates_and_verifies() -> None:
    orchestrator, repository, target = _engine_fixture()

    status = asyncio.run(orchestrator.execute("run"))

    assert status is RunStatus.SUCCEEDED
    assert repository.sequence.index("persist_plan") < next(
        index for index, value in enumerate(repository.sequence) if value.startswith("start:")
    )
    assert [kind for kind, _ in target.writes].count("create_song") == 1
    assert "create_arrangement" not in [kind for kind, _ in target.writes]
    assert all(execution.status.value == "verified" for execution in repository.executions.values())


def test_default_mode_skips_unchanged_worshiptools_event_after_first_sync() -> None:
    start = dt("2026-01-01T10:00:00Z")
    source_event = SourceEvent("service", "Service", (start,), ("song",))
    source = FakeSourceProvider(
        (source_event,), (SourceSong("song", "Amazing Grace", "John Newton", "123"),)
    )
    target_event = TargetEvent("event", "Event", start)
    target = FakeTargetProvider(
        (target_event,),
        (target_song("desired", "123"),),
        {
            "event": Agenda(
                "event",
                (
                    AgendaItem("header", 0, "header", "Lobpreis"),
                    AgendaItem(
                        "desired-item",
                        1,
                        "song",
                        song_id="desired",
                        arrangement_id="arr-desired",
                    ),
                ),
            )
        },
    )
    first_repository = MemoryRunRepository(
        RunSpecification("first-run", "workspace", "wt", "ct", profile())
    )
    first = SyncOrchestrator(
        first_repository,
        StaticProviderRegistry(source, target),
        clock=type("Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")})(),
        event_leases=MemoryEventLeaseManager(),
    )

    assert asyncio.run(first.execute("first-run")) is RunStatus.SUCCEEDED
    checkpoints = tuple(first_repository.event_sync_rows.values())
    assert len(checkpoints) == 1

    async def agenda_must_not_be_loaded(event_id: str) -> Agenda:
        raise AssertionError(f"unchanged agenda was loaded: {event_id}")

    target.get_agenda = agenda_must_not_be_loaded  # type: ignore[method-assign]
    second_repository = MemoryRunRepository(
        RunSpecification("second-run", "workspace", "wt", "ct", profile()),
        event_sync_states=checkpoints,
    )
    second = SyncOrchestrator(
        second_repository,
        StaticProviderRegistry(source, target),
        clock=type("Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")})(),
        event_leases=MemoryEventLeaseManager(),
    )

    assert asyncio.run(second.execute("second-run")) is RunStatus.SKIPPED
    assert second_repository.plan is not None
    assert second_repository.plan.events[0].status is EventPlanStatus.SKIPPED
    assert second_repository.plan.events[0].issues[0].code == "source_unchanged"


def test_enforce_source_mode_reconciles_even_with_matching_checkpoint() -> None:
    start = dt("2026-01-01T10:00:00Z")
    configured = profile(sync_mode=SyncMode.ENFORCE_SOURCE)
    source_event = SourceEvent("service", "Service", (start,), ("song",))
    source = FakeSourceProvider((source_event,), (source_song("song", "123"),))
    target_event = TargetEvent("event", "Event", start)
    desired = target_song("desired", "123")
    old = TargetSong(
        "old", "Old", "Artist", "999", (Arrangement("old-arr", "Old", True),)
    )
    checkpoint = EventSyncCheckpoint(
        source_event_id="service",
        target_event_id="event",
        source_fingerprint=source_event_fingerprint(source_event, source.songs),
        config_fingerprint=sync_config_fingerprint(
            configured, source_connection_id="wt", target_connection_id="ct"
        ),
    )
    repository = MemoryRunRepository(
        RunSpecification("forced-run", "workspace", "wt", "ct", configured),
        event_sync_states=(checkpoint,),
    )
    target = FakeTargetProvider(
        (target_event,),
        (desired, old),
        {
            "event": Agenda(
                "event",
                (
                    AgendaItem("header", 0, "header", "Lobpreis"),
                    AgendaItem(
                        "slot", 1, "song", song_id="old", arrangement_id="old-arr"
                    ),
                ),
            )
        },
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source, target),
        clock=type("Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")})(),
        event_leases=MemoryEventLeaseManager(),
    )

    assert asyncio.run(orchestrator.execute("forced-run")) is RunStatus.SUCCEEDED
    assert [kind for kind, _ in target.writes] == ["replace_item"]
    assert target.agendas["event"].items[1].song_id == "desired"


def test_relevant_profile_change_causes_one_new_default_mode_sync() -> None:
    start = dt("2026-01-01T10:00:00Z")
    configured = profile(arrangement_name="New arrangement default")
    previous = profile(arrangement_name="Previous arrangement default")
    source_event = SourceEvent("service", "Service", (start,), ("song",))
    source = FakeSourceProvider((source_event,), (source_song("song", "123"),))
    checkpoint = EventSyncCheckpoint(
        source_event_id="service",
        target_event_id="event",
        source_fingerprint=source_event_fingerprint(source_event, source.songs),
        config_fingerprint=sync_config_fingerprint(
            previous, source_connection_id="wt", target_connection_id="ct"
        ),
    )
    repository = MemoryRunRepository(
        RunSpecification("config-run", "workspace", "wt", "ct", configured),
        event_sync_states=(checkpoint,),
    )
    desired = target_song("desired", "123")
    target = FakeTargetProvider(
        (TargetEvent("event", "Event", start),),
        (desired,),
        {
            "event": Agenda(
                "event",
                (
                    AgendaItem("header", 0, "header", "Lobpreis"),
                    AgendaItem(
                        "slot",
                        1,
                        "song",
                        song_id="desired",
                        arrangement_id="arr-desired",
                    ),
                ),
            )
        },
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source, target),
        clock=type("Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")})(),
        event_leases=MemoryEventLeaseManager(),
    )

    assert asyncio.run(orchestrator.execute("config-run")) is RunStatus.SUCCEEDED
    assert repository.plan is not None
    assert repository.plan.events[0].status is EventPlanStatus.READY
    assert repository.event_sync_rows[("service", "event")].config_fingerprint == (
        sync_config_fingerprint(
            configured, source_connection_id="wt", target_connection_id="ct"
        )
    )


def test_dry_run_persists_plan_but_never_applies_it() -> None:
    orchestrator, repository, target = _engine_fixture(dry_run=True)

    status = asyncio.run(orchestrator.execute("run"))

    assert status is RunStatus.SUCCEEDED
    assert repository.plan is not None
    assert target.writes == []
    assert repository.executions == {}
    assert repository.event_sync_rows == {}


def _missing_agenda_fixture(*, dry_run: bool = False):
    start = dt("2026-01-01T10:00:00Z")
    source = FakeSourceProvider(
        (SourceEvent("service", "Service", (start,), ("new",)),),
        (SourceSong("new", "New Song", "Artist", "777"),),
    )
    repository = MemoryRunRepository(
        RunSpecification(
            "missing-agenda", "workspace", "wt", "ct", profile(), dry_run=dry_run
        )
    )
    target = FakeTargetProvider(
        (TargetEvent("event", "Event", start),),
        (),
        {},
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source, target),
        clock=type(
            "Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")}
        )(),
        event_leases=MemoryEventLeaseManager(),
    )
    return orchestrator, repository, target


def test_event_without_agenda_is_skipped_without_provider_writes() -> None:
    orchestrator, repository, target = _missing_agenda_fixture()

    status = asyncio.run(orchestrator.execute("missing-agenda"))

    assert status is RunStatus.SKIPPED
    assert repository.error is None
    assert repository.plan is not None
    assert repository.plan.preparation_actions == ()
    event = repository.plan.events[0]
    assert event.status is EventPlanStatus.SKIPPED
    assert event.actions == ()
    assert event.issues[0].code == "agenda_missing"
    assert event.issues[0].severity is IssueSeverity.WARNING
    assert target.writes == []
    assert repository.executions == {}
    assert repository.event_sync_rows == {}


def test_dry_run_without_agenda_is_skipped_without_provider_writes() -> None:
    orchestrator, repository, target = _missing_agenda_fixture(dry_run=True)

    status = asyncio.run(orchestrator.execute("missing-agenda"))

    assert status is RunStatus.SKIPPED
    assert repository.plan is not None
    assert repository.plan.events[0].status is EventPlanStatus.SKIPPED
    assert repository.plan.preparation_actions == ()
    assert target.writes == []
    assert repository.executions == {}


def test_missing_agenda_does_not_prevent_another_event_from_succeeding() -> None:
    starts = (dt("2026-01-01T10:00:00Z"), dt("2026-01-01T11:00:00Z"))
    source = FakeSourceProvider(
        tuple(
            SourceEvent(f"source-{index}", "Service", (start,), ("new",))
            for index, start in enumerate(starts)
        ),
        (SourceSong("new", "New Song", "Artist", "777"),),
    )
    repository = MemoryRunRepository(
        RunSpecification("mixed-agendas", "workspace", "wt", "ct", profile())
    )
    target = FakeTargetProvider(
        tuple(
            TargetEvent(event_id, "Event", start)
            for event_id, start in zip(("missing", "ready"), starts, strict=True)
        ),
        (),
        {"ready": Agenda("ready", (AgendaItem("header", 0, "header", "Lobpreis"),))},
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source, target),
        clock=type(
            "Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")}
        )(),
        event_leases=MemoryEventLeaseManager(),
    )

    status = asyncio.run(orchestrator.execute("mixed-agendas"))

    assert status is RunStatus.SUCCEEDED
    assert repository.plan is not None
    assert [event.status for event in repository.plan.events] == [
        EventPlanStatus.SKIPPED,
        EventPlanStatus.READY,
    ]
    assert target.agendas["ready"].items[1].song_id is not None
    assert "missing" not in target.agendas


def test_non_not_found_agenda_error_still_fails_the_run() -> None:
    orchestrator, repository, target = _missing_agenda_fixture()

    async def forbidden_agenda(event_id: str) -> Agenda:
        raise AuthorizationError(event_id=event_id)

    target.get_agenda = forbidden_agenda  # type: ignore[method-assign]

    status = asyncio.run(orchestrator.execute("missing-agenda"))

    assert status is RunStatus.FAILED
    assert repository.plan is None
    assert repository.error is not None
    assert repository.error["kind"] == "authorization"


def test_planning_renews_run_lease_while_provider_reads_are_in_flight() -> None:
    start = dt("2026-01-01T10:00:00Z")

    class SlowSource(FakeSourceProvider):
        async def list_events(self, start, end):
            await asyncio.sleep(0.25)
            return await super().list_events(start, end)

    repository = MemoryRunRepository(
        RunSpecification(
            "heartbeat-run", "workspace", "wt", "ct", profile(), dry_run=True
        )
    )
    source = SlowSource(
        (SourceEvent("service", "Service", (start,), ("song",)),),
        (source_song("song", "123"),),
    )
    target = FakeTargetProvider(
        (TargetEvent("event", "Event", start),),
        (target_song("target", "123"),),
        {
            "event": Agenda(
                "event",
                (
                    AgendaItem("header", 0, "header", "Lobpreis"),
                    AgendaItem(
                        "song-item",
                        1,
                        "song",
                        song_id="target",
                        arrangement_id="arr-target",
                    ),
                ),
            )
        },
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source, target),
        clock=type(
            "Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")}
        )(),
        lease_seconds=0.3,
        event_leases=MemoryEventLeaseManager(),
    )

    assert asyncio.run(orchestrator.execute("heartbeat-run")) is RunStatus.SUCCEEDED
    assert repository.renewals >= 1


def test_remote_catalog_and_event_leases_are_renewed_during_slow_writes() -> None:
    start = dt("2026-01-01T10:00:00Z")

    class SlowTarget(FakeTargetProvider):
        async def create_song(self, payload, action_id):
            await asyncio.sleep(0.35)
            return await super().create_song(payload, action_id)

        async def insert_agenda_song(
            self,
            event_id,
            arrangement_id,
            defaults,
            action_id,
            *,
            before_item_id=None,
            after_item_id=None,
        ):
            await asyncio.sleep(0.35)
            return await super().insert_agenda_song(
                event_id,
                arrangement_id,
                defaults,
                action_id,
                before_item_id=before_item_id,
                after_item_id=after_item_id,
            )

    class CountingLeases(MemoryEventLeaseManager):
        def __init__(self):
            super().__init__()
            self.renewals: dict[tuple[str, str], int] = {}

        async def renew(
            self, connection_id, event_id, owner_token, ttl_seconds
        ):
            key = (connection_id, event_id)
            self.renewals[key] = self.renewals.get(key, 0) + 1
            return await super().renew(
                connection_id, event_id, owner_token, ttl_seconds
            )

    source_event = SourceEvent("service", "Service", (start,), ("new",))
    repository = MemoryRunRepository(
        RunSpecification("lease-heartbeat", "workspace", "wt", "ct", profile())
    )
    target = SlowTarget(
        (TargetEvent("event", "Event", start),),
        (),
        {
            "event": Agenda(
                "event", (AgendaItem("header", 0, "header", "Lobpreis"),)
            )
        },
    )
    leases = CountingLeases()
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(
            FakeSourceProvider(
                (source_event,),
                (SourceSong("new", "New Song", "Artist", "777"),),
            ),
            target,
        ),
        clock=type(
            "Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")}
        )(),
        lease_seconds=0.3,
        event_leases=leases,
    )

    assert asyncio.run(orchestrator.execute("lease-heartbeat")) is RunStatus.SUCCEEDED
    assert leases.renewals[("ct", "__song_catalog__")] >= 2
    assert leases.renewals[("ct", "event")] >= 2


def test_identical_foreign_song_remains_unowned_after_successful_noop() -> None:
    start = dt("2026-01-01T10:00:00Z")
    source_event = SourceEvent("service", "Service", (start,), ("song",))
    target_event = TargetEvent("event", "Event", start)
    song = target_song("target", "123")
    repository = MemoryRunRepository(
        RunSpecification("noop-run", "workspace", "wt", "ct", profile())
    )
    target = FakeTargetProvider(
        (target_event,),
        (song,),
        {
            "event": Agenda(
                "event",
                (
                    AgendaItem("header", 0, "header", "Lobpreis"),
                    AgendaItem(
                        "foreign",
                        1,
                        "song",
                        song_id="target",
                        arrangement_id="arr-target",
                    ),
                ),
            )
        },
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(
            FakeSourceProvider((source_event,), (source_song("song", "123"),)),
            target,
        ),
        clock=type("Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")})(),
        event_leases=MemoryEventLeaseManager(),
    )

    status = asyncio.run(orchestrator.execute("noop-run"))

    assert status is RunStatus.SUCCEEDED
    assert target.writes == []
    assert repository.ownership_rows == []


def test_cleanup_does_not_delete_owned_item_changed_by_a_user() -> None:
    start = dt("2026-01-01T10:00:00Z")
    # The second, previously synced slot was removed from WorshipTools while
    # the first slot remains part of the service.
    source_event = SourceEvent("service", "Service", (start,), ("retained",))
    target_event = TargetEvent("event", "Event", start)
    retained_item = AgendaItem(
        "retained-slot",
        1,
        "song",
        song_id="retained-target",
        arrangement_id="arr-retained-target",
    )
    changed_item = AgendaItem(
        "owned-slot",
        2,
        "song",
        song_id="song-b",
        arrangement_id="arrangement-b",
    )
    ownership = Ownership(
        "profile-1",
        "event",
        "owned-slot",
        "main:0:removed-song",
        "main",
        {
            "target_song_id": "song-a",
            "arrangement_id": "arrangement-a",
        },
    )
    retained_ownership = Ownership(
        "profile-1",
        "event",
        "retained-slot",
        "main:0:retained",
        "main",
        {
            "target_song_id": "retained-target",
            "arrangement_id": "arr-retained-target",
        },
    )
    repository = MemoryRunRepository(
        RunSpecification("safe-cleanup", "workspace", "wt", "ct", profile()),
        (retained_ownership, ownership),
    )
    target = FakeTargetProvider(
        (target_event,),
        (target_song("retained-target", "123"),),
        {
            "event": Agenda(
                "event",
                (
                    AgendaItem("header", 0, "header", "Lobpreis"),
                    retained_item,
                    changed_item,
                ),
            )
        },
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(
            FakeSourceProvider(
                (source_event,), (source_song("retained", "123"),)
            ),
            target,
        ),
        clock=type(
            "Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")}
        )(),
        event_leases=MemoryEventLeaseManager(),
    )

    status = asyncio.run(orchestrator.execute("safe-cleanup"))

    assert status is RunStatus.SUCCEEDED
    event_plan = repository.plan.events[0]
    assert event_plan.status is EventPlanStatus.READY
    assert event_plan.issues[0].code == "owned_agenda_item_changed"
    assert event_plan.issues[0].severity is IssueSeverity.WARNING
    cleanup = next(
        action
        for action in event_plan.actions
        if action.payload.get("agenda_item_id") == "owned-slot"
    )
    assert cleanup.kind is ActionKind.NOOP
    assert cleanup.payload["cleanup_only"] is True
    assert target.writes == []
    assert target.agendas["event"].items[2] == changed_item
    assert all(row.agenda_item_id != "owned-slot" for row in repository.ownership_rows)


def test_one_failed_event_does_not_prevent_other_event_and_run_is_partial() -> None:
    starts = (dt("2026-01-01T10:00:00Z"), dt("2026-01-01T11:00:00Z"))
    source_events = tuple(
        SourceEvent(f"source-{index}", "Service", (start,), ("song",)) for index, start in enumerate(starts)
    )
    target_events = tuple(TargetEvent(f"target-{index}", "Event", start) for index, start in enumerate(starts))
    desired = target_song("desired", "123")
    old = TargetSong("old", "Old", "Old", "999", (Arrangement("old-arr", "Old", True),))
    source_provider = FakeSourceProvider(source_events, (source_song("song", "123"),))
    repository = MemoryRunRepository(RunSpecification("run", "workspace", "wt", "ct", profile()))
    agendas = {
        event.id: Agenda(
            event.id,
            (
                AgendaItem(f"header-{event.id}", 0, "header", "Lobpreis"),
                AgendaItem(f"old-{event.id}", 1, "song", song_id="old", arrangement_id="old-arr"),
            ),
        )
        for event in target_events
    }
    target_provider = FakeTargetProvider(
        target_events, (desired, old), agendas, fail_event_ids={"target-0"}
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source_provider, target_provider),
        clock=type("Clock", (), {"now": lambda self: dt("2026-01-01T00:00:00Z")})(),
        event_leases=MemoryEventLeaseManager(),
    )

    status = asyncio.run(orchestrator.execute("run"))

    assert status is RunStatus.PARTIAL
    assert target_provider.agendas["target-1"].items[1].song_id == "desired"
    assert set(repository.event_sync_rows) == {("source-1", "target-1")}
    action_ids = [action.id for event in repository.plan.events for action in event.actions]
    assert len(action_ids) == len(set(action_ids))


def test_persisted_plan_reconciles_insert_committed_before_worker_crash() -> None:
    start = dt("2026-01-01T10:00:00Z")
    source_event = SourceEvent("source", "Service", (start,), ("song",))
    target_event = TargetEvent("target", "Event", start)
    song = target_song("desired", "123")
    initial = Agenda(
        "target",
        (
            AgendaItem("header", 0, "header", "Lobpreis"),
            AgendaItem("text", 1, "text", "Predigt"),
        ),
    )
    persisted = SyncPlanner().plan(
        run_id="recovery-run",
        profile=profile(),
        created_at=dt("2026-01-01T00:00:00Z"),
        source_events=(source_event,),
        target_events=(target_event,),
        source_songs=(source_song("song", "123"),),
        target_songs=(song,),
        agendas={"target": initial},
        ownerships={},
    )
    # Simulate: ChurchTools committed INSERT, then the worker died before it
    # could persist APPLIED.  The next worker must adopt, not duplicate, it.
    committed = Agenda(
        "target",
        (
            AgendaItem("header", 0, "header", "Lobpreis"),
            AgendaItem("committed", 1, "song", song_id="desired", arrangement_id="arr-desired"),
            AgendaItem("text", 2, "text", "Predigt"),
        ),
    )
    repository = MemoryRunRepository(
        RunSpecification("recovery-run", "workspace", "wt", "ct", profile())
    )
    repository.plan = sync_plan_from_dict(persisted.as_dict())
    target = FakeTargetProvider((target_event,), (song,), {"target": committed})
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(FakeSourceProvider((), ()), target),
        event_leases=MemoryEventLeaseManager(),
    )

    status = asyncio.run(orchestrator.execute("recovery-run"))

    assert status is RunStatus.SUCCEEDED
    assert target.writes == []
    assert [item.id for item in target.agendas["target"].items].count("committed") == 1
    assert repository.ownership_rows[0].agenda_item_id == "committed"


def test_insert_reconciliation_never_claims_preexisting_adjacent_song() -> None:
    start = dt("2026-01-01T10:00:00Z")
    source_event = SourceEvent("source", "Service", (start,), ("song",))
    target_event = TargetEvent("target", "Event", start)
    song = target_song("desired", "123")
    insert_before_text = ProfileConfig(
        id="profile-1",
        revision=3,
        source_timezone="Europe/Berlin",
        target_timezone="Europe/Berlin",
        match_mode=MatchMode.EXACT_TIME,
        placements=(
            PlacementRule(
                "main",
                AgendaAnchor(item_type="text", title="Predigt"),
                AnchorRelation.BEFORE,
            ),
        ),
        song_category_id=7,
    )
    initial = Agenda(
        "target",
        (
            AgendaItem("header", 0, "header", "Lobpreis"),
            AgendaItem(
                "human-song",
                1,
                "song",
                song_id="desired",
                arrangement_id="arr-desired",
            ),
            AgendaItem("text", 2, "text", "Predigt"),
        ),
    )
    persisted = SyncPlanner().plan(
        run_id="preexisting-adjacent",
        profile=insert_before_text,
        created_at=dt("2026-01-01T00:00:00Z"),
        source_events=(source_event,),
        target_events=(target_event,),
        source_songs=(source_song("song", "123"),),
        target_songs=(song,),
        agendas={"target": initial},
        ownerships={},
    )
    action = persisted.events[0].actions[0]
    assert action.kind is ActionKind.INSERT_ITEM
    assert "human-song" in action.payload["initial_agenda_item_ids"]

    # Only an unrelated human edit happened after planning. The existing song
    # still sits immediately before the anchor but must never be adopted.
    concurrently_edited = Agenda(
        "target",
        initial.items
        + (AgendaItem("unrelated", 3, "text", "Hinweis"),),
    )
    repository = MemoryRunRepository(
        RunSpecification(
            "preexisting-adjacent",
            "workspace",
            "wt",
            "ct",
            insert_before_text,
        )
    )
    repository.plan = sync_plan_from_dict(persisted.as_dict())
    target = FakeTargetProvider(
        (target_event,), (song,), {"target": concurrently_edited}
    )
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(FakeSourceProvider((), ()), target),
        event_leases=MemoryEventLeaseManager(),
    )

    assert (
        asyncio.run(orchestrator.execute("preexisting-adjacent"))
        is RunStatus.FAILED
    )
    assert target.writes == []
    assert repository.ownership_rows == []


def test_verified_action_repairs_missing_ownership_after_crash() -> None:
    start = dt("2026-01-01T10:00:00Z")
    source_event = SourceEvent("source", "Service", (start,), ("song",))
    target_event = TargetEvent("target", "Event", start)
    song = target_song("desired", "123")
    initial = Agenda(
        "target",
        (
            AgendaItem("header", 0, "header", "Lobpreis"),
            AgendaItem("text", 1, "text", "Predigt"),
        ),
    )
    persisted = SyncPlanner().plan(
        run_id="ownership-recovery",
        profile=profile(),
        created_at=dt("2026-01-01T00:00:00Z"),
        source_events=(source_event,),
        target_events=(target_event,),
        source_songs=(source_song("song", "123"),),
        target_songs=(song,),
        agendas={"target": initial},
        ownerships={},
    )
    committed = Agenda(
        "target",
        (
            AgendaItem("header", 0, "header", "Lobpreis"),
            AgendaItem(
                "committed",
                1,
                "song",
                song_id="desired",
                arrangement_id="arr-desired",
            ),
            AgendaItem("text", 2, "text", "Predigt"),
        ),
    )
    action = persisted.events[0].actions[0]
    repository = MemoryRunRepository(
        RunSpecification(
            "ownership-recovery", "workspace", "wt", "ct", profile()
        )
    )
    repository.plan = sync_plan_from_dict(persisted.as_dict())
    # Model the exact crash point: VERIFIED committed, bind_ownership not yet
    # executed. A redelivery must reverify before recreating the binding.
    repository.executions[action.id] = ActionExecution(
        action.id,
        ActionStatus.VERIFIED,
        result={
            "agenda_item_id": "committed",
            "resource_key": action.payload["resource_key"],
            "target_song_id": "desired",
            "arrangement_id": "arr-desired",
            "agenda_fingerprint": committed.fingerprint,
        },
    )
    target = FakeTargetProvider((target_event,), (song,), {"target": committed})
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(FakeSourceProvider((), ()), target),
        event_leases=MemoryEventLeaseManager(),
    )

    assert (
        asyncio.run(orchestrator.execute("ownership-recovery"))
        is RunStatus.SUCCEEDED
    )
    assert target.writes == []
    assert len(repository.ownership_rows) == 1
    assert repository.ownership_rows[0].agenda_item_id == "committed"
