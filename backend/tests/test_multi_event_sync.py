from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.sync.engine import SyncOrchestrator
from app.sync.matching import match_events
from app.sync.models import (
    Agenda,
    AgendaAnchor,
    AgendaItem,
    Arrangement,
    EventPlanStatus,
    EventSelector,
    MatchMode,
    PlacementRule,
    ProfileConfig,
    RunSpecification,
    RunStatus,
    SourceEvent,
    SourceSong,
    TargetEvent,
    TargetSong,
    stable_action_id,
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


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
STARTS = (NOW.replace(hour=9), NOW.replace(hour=11))
SOURCE = SourceEvent("wt", "Service", STARTS, ("one", "two"))
TARGETS = tuple(
    TargetEvent(f"ct-{i}", "Gottesdienst", start) for i, start in enumerate(STARTS)
)
SONGS = (
    SourceSong("one", "First Song", "Artist", "111"),
    SourceSong("two", "Second Song", "Artist", "222"),
)
PROFILE = ProfileConfig(
    id="profile",
    revision=1,
    source_timezone="Europe/Berlin",
    target_timezone="Europe/Berlin",
    placements=(PlacementRule("main", AgendaAnchor(item_type="header", title="Lobpreis")),),
    song_category_id=7,
)


@pytest.mark.parametrize("mode", list(MatchMode))
def test_one_source_matches_each_target_once_after_filters(mode):
    source = replace(SOURCE, starts_at=(*STARTS, STARTS[0]))
    extra = replace(TARGETS[0], id="filtered", name="Probe")
    configured = replace(
        PROFILE, match_mode=mode,
        selectors=(EventSelector(name_contains="Gottesdienst"),),
    )

    matches = match_events(configured, (source,), (*reversed(TARGETS), extra))

    assert [match.target.id for match in matches] == ["ct-0", "ct-1"]
    assert all(match.status is EventPlanStatus.READY for match in matches)


def test_date_matching_includes_all_events_on_each_local_date_even_at_same_time():
    source = replace(SOURCE, starts_at=(NOW, NOW.replace(day=2)))
    targets = (
        *TARGETS,
        replace(TARGETS[0], id="same-time"),
        TargetEvent("next-day", "Event", NOW.replace(day=2, hour=20)),
        TargetEvent("outside", "Event", NOW.replace(day=3, hour=20)),
    )

    matches = match_events(replace(PROFILE, match_mode=MatchMode.DATE_ONLY), (source,), targets)

    assert {match.target.id for match in matches} == {"ct-0", "ct-1", "same-time", "next-day"}
    assert all(match.status is EventPlanStatus.READY for match in matches)


def test_time_conflicts_are_aggregated_without_blocking_other_times():
    source = replace(SOURCE, starts_at=(*STARTS, NOW.replace(hour=13)))
    duplicate = replace(
        TARGETS[0], id="duplicate",
        starts_at=datetime.fromisoformat("2026-01-01T10:00:00+01:00"),
    )
    targets = (
        *TARGETS, duplicate,
        TargetEvent("a", "Event", NOW.replace(hour=13)),
        TargetEvent("b", "Event", NOW.replace(hour=13)),
    )

    matches = match_events(PROFILE, (source,), targets)

    assert len(matches) == 2
    conflict, ready = matches
    assert conflict.target is None
    assert conflict.status is EventPlanStatus.AMBIGUOUS
    assert [issue.code for issue in conflict.issues] == ["event_ambiguous", "event_ambiguous"]
    assert conflict.issues[0].details == {
        "starts_at": STARTS[0].isoformat(), "candidate_ids": ["ct-0", "duplicate"],
    }
    assert ready.target.id == "ct-1"
    assert ready.status is EventPlanStatus.READY


@pytest.mark.parametrize("mode", list(MatchMode))
def test_shared_targets_are_blocked_but_independent_targets_remain_ready(mode):
    # In date mode the sources overlap on day 1; only the first also has day 2.
    second_time = STARTS[1] if mode is MatchMode.EXACT_TIME else STARTS[1].replace(day=2)
    source = replace(SOURCE, starts_at=(STARTS[0], second_time))
    other = replace(SOURCE, id="other", starts_at=(STARTS[0],))
    targets = (TARGETS[0], replace(TARGETS[1], starts_at=second_time))

    matches = match_events(replace(PROFILE, match_mode=mode), (source, other), targets)

    ready = [match for match in matches if match.target]
    assert [(match.source.id, match.target.id) for match in ready] == [("wt", "ct-1")]
    conflicts = [match for match in matches if match.status is EventPlanStatus.AMBIGUOUS]
    assert {match.source.id for match in conflicts} == {"wt", "other"}
    assert all(match.issues[0].code == "target_event_reused" for match in conflicts)


def test_missing_time_does_not_block_existing_match():
    matches = match_events(PROFILE, (SOURCE,), TARGETS[:1])
    assert len(matches) == 1
    assert matches[0].target == TARGETS[0]
    missing = match_events(PROFILE, (SOURCE,), ())
    assert missing[0].issues[0].code == "event_not_found"


def make_target(*, existing_songs=False):
    songs = tuple(
        TargetSong(
            song.id, song.name, song.artist, song.ccli,
            (Arrangement(f"arr-{song.id}", "Standard", True),),
        )
        for song in SONGS
    ) if existing_songs else ()
    return FakeTargetProvider(
        TARGETS,
        songs,
        {
            event.id: Agenda(
                event.id, (AgendaItem(f"header-{event.id}", 0, "header", "Lobpreis"),)
            )
            for event in TARGETS
        },
    )


def execute(repository, target, source=None):
    orchestrator = SyncOrchestrator(
        repository,
        StaticProviderRegistry(source or FakeSourceProvider((SOURCE,), SONGS), target),
        clock=type("Clock", (), {"now": lambda self: NOW})(),
        event_leases=MemoryEventLeaseManager(),
    )
    return asyncio.run(orchestrator.execute(repository.specification.run_id))


@pytest.mark.parametrize("mode", list(MatchMode))
@pytest.mark.parametrize("preparation", ["songs", "arrangements"])
def test_both_agendas_share_preparation_and_keep_independent_checkpoints(mode, preparation):
    configured = replace(PROFILE, match_mode=mode)
    target = make_target(existing_songs=preparation == "arrangements")
    if preparation == "arrangements":
        target.songs = {song.id: replace(song, arrangements=()) for song in target.songs.values()}
    repository = MemoryRunRepository(RunSpecification("run", "workspace", "wt", "ct", configured))

    assert execute(repository, target) is RunStatus.SUCCEEDED
    expected = "create_song" if preparation == "songs" else "create_arrangement"
    assert [kind for kind, _ in target.writes].count(expected) == 2
    assert len(repository.plan.preparation_actions) == 2
    song_order = [item.song_id for item in target.agendas["ct-0"].items[1:]]
    assert [target.songs[song_id].ccli for song_id in song_order] == ["111", "222"]
    assert [item.song_id for item in target.agendas["ct-1"].items[1:]] == song_order
    assert set(repository.event_sync_rows) == {("wt", "ct-0"), ("wt", "ct-1")}
    assert {row.target_event_id for row in repository.ownership_rows} == {"ct-0", "ct-1"}
    assert len({event.id for event in repository.plan.events}) == 2
    action_ids = [action.id for event in repository.plan.events for action in event.actions]
    assert len(action_ids) == len(set(action_ids)) == 4

    async def no_agenda_load(event_id):
        raise AssertionError(f"Unchanged target loaded: {event_id}")

    target.get_agenda = no_agenda_load
    following = MemoryRunRepository(
        RunSpecification("following", "workspace", "wt", "ct", configured),
        event_sync_states=tuple(repository.event_sync_rows.values()),
    )
    writes = list(target.writes)
    assert execute(following, target) is RunStatus.SKIPPED
    assert len(following.plan.events) == 2
    assert all(event.issues[0].code == "source_unchanged" for event in following.plan.events)
    assert target.writes == writes


@pytest.mark.parametrize("failure", ["missing_agenda", "write_error", "ambiguous_time"])
def test_one_unavailable_target_does_not_block_other_target(failure):
    target = make_target()
    if failure == "missing_agenda":
        del target.agendas["ct-0"]
    elif failure == "write_error":
        target.fail_event_ids.add("ct-0")
    else:
        target.events += (replace(TARGETS[0], id="duplicate"),)
    repository = MemoryRunRepository(RunSpecification("run", "workspace", "wt", "ct", PROFILE))

    expected = RunStatus.SUCCEEDED if failure == "missing_agenda" else RunStatus.PARTIAL
    assert execute(repository, target) is expected
    assert len(target.agendas["ct-1"].items) == 3
    assert set(repository.event_sync_rows) == {("wt", "ct-1")}

    if failure == "write_error":
        target.fail_event_ids.clear()
        following = MemoryRunRepository(
            RunSpecification("following", "workspace", "wt", "ct", PROFILE),
            event_sync_states=tuple(repository.event_sync_rows.values()),
        )
        assert execute(following, target) is RunStatus.SUCCEEDED
        assert set(following.event_sync_rows) == {("wt", "ct-0"), ("wt", "ct-1")}
        assert following.plan.events[1].issues[0].code == "source_unchanged"


def test_plan_is_deterministic_and_recovery_adopts_committed_insert_per_target():
    target = make_target(existing_songs=True)
    inputs = dict(
        run_id="recovery", profile=PROFILE, created_at=NOW,
        source_events=(SOURCE,), target_events=TARGETS, source_songs=SONGS,
        target_songs=tuple(target.songs.values()), agendas=target.agendas, ownerships={},
    )
    plan = SyncPlanner().plan(**inputs)
    reordered = SyncPlanner().plan(**{**inputs, "target_events": tuple(reversed(TARGETS))})
    assert plan.fingerprint == reordered.fingerprint
    assert sync_plan_from_dict(plan.as_dict()) == plan

    # The first insert reached CT, but the worker died before recording it.
    action = plan.events[0].actions[0]
    asyncio.run(target.insert_agenda_song(
        "ct-0", action.payload["arrangement_id"], {}, action.id,
        after_item_id="header-ct-0",
    ))
    target.writes.clear()
    repository = MemoryRunRepository(RunSpecification("recovery", "workspace", "wt", "ct", PROFILE))
    repository.plan = sync_plan_from_dict(plan.as_dict())

    assert execute(repository, target, FakeSourceProvider((), ())) is RunStatus.SUCCEEDED
    assert [kind for kind, _ in target.writes] == ["insert_item"] * 3
    for agenda in target.agendas.values():
        assert [item.song_id for item in agenda.items[1:]] == ["one", "two"]
    assert len(repository.ownership_rows) == 4


def test_legacy_single_target_plan_keeps_persisted_ids():
    target = make_target(existing_songs=True)
    plan = SyncPlanner().plan(
        run_id="legacy", profile=PROFILE, created_at=NOW,
        source_events=(SOURCE,), target_events=TARGETS[:1], source_songs=SONGS,
        target_songs=tuple(target.songs.values()), agendas=target.agendas, ownerships={},
    )
    event = plan.events[0]
    legacy_event_id = stable_action_id("legacy", "event", "wt")
    legacy_actions = tuple(
        replace(
            action,
            id=stable_action_id("legacy", action.kind.value, legacy_event_id, action.payload["source_key"]),
            event_plan_id=legacy_event_id,
        )
        for action in event.actions
    )
    legacy_plan = replace(plan, events=(replace(event, id=legacy_event_id, actions=legacy_actions),))
    repository = MemoryRunRepository(RunSpecification("legacy", "workspace", "wt", "ct", PROFILE))
    repository.plan = sync_plan_from_dict(legacy_plan.as_dict())

    assert execute(repository, target, FakeSourceProvider((), ())) is RunStatus.SUCCEEDED
    assert repository.plan.fingerprint == legacy_plan.fingerprint
    assert set(repository.executions) == {action.id for action in legacy_actions}
    assert [item.song_id for item in target.agendas["ct-0"].items[1:]] == ["one", "two"]
    assert len(target.agendas["ct-1"].items) == 1
