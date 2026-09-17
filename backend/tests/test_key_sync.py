from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.sync.engine import SyncOrchestrator, _reconcile_agenda_effect
from app.sync.fingerprints import source_event_fingerprint
from app.sync.keys import arrangement_name, normalize_key
from app.sync.models import (
    ActionKind, ActionExecution, ActionStatus, Agenda, AgendaAnchor, AgendaItem,
    Arrangement, EventPlanStatus, PlacementRule, ProfileConfig, RunSpecification,
    RunStatus, SourceEvent, SourceSong, SyncMode, TargetEvent, TargetSong,
)
from app.sync.planner import SyncPlanner
from app.sync.serialization import sync_plan_from_dict
from app.sync.testing import FakeSourceProvider, FakeTargetProvider, MemoryRunRepository, StaticProviderRegistry

NOW = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
PROFILE = ProfileConfig("profile", 1, "UTC", "UTC", song_category_id=1,
                        placements=(PlacementRule("main", AgendaAnchor(item_id="header")),))
SONG = SourceSong("s", "Song", "Artist", "123", "C")
EVENT = SourceEvent("service", "Service", (NOW,), ("s",), ("D",))
TARGET_EVENT = TargetEvent("event", "Service", NOW)


def target_provider(songs=()):
    return FakeTargetProvider((TARGET_EVENT,), songs, {
        "event": Agenda("event", (AgendaItem("header", 0, "header", "Songs"),)),
    })


def plan(events=(EVENT,), source_songs=(SONG,), target=None):
    target = target or target_provider()
    return SyncPlanner().plan(
        run_id="run", profile=PROFILE, created_at=NOW, source_events=events,
        target_events=target.events, source_songs=source_songs,
        target_songs=tuple(target.songs.values()), agendas=target.agendas, ownerships={},
    )


def execute(repository, target, source):
    return asyncio.run(SyncOrchestrator(
        repository, StaticProviderRegistry(source, target),
        clock=type("Clock", (), {"now": lambda self: NOW})(),
    ).execute(repository.specification.run_id))


@pytest.mark.parametrize(("raw", "expected"), [
    (" G ♭ ", "F#"), ("c#", "Db"), ("D♭m", "C#m"), ("A#m", "Bbm"),
    ("aM", "Am"), ("B", "B"), ("B#", "C"), ("Cb", "B"), ("E#m", "Fm"),
    (None, None), ("", None), ("H", None), ("banana", None), ("Cm7", None),
])
def test_keys_are_normalized_to_churchtools_values(raw, expected):
    assert normalize_key(raw) == expected


def test_key_changes_affect_fingerprints_but_equivalent_spellings_do_not():
    original = source_event_fingerprint(EVENT, (SONG,))
    assert original != source_event_fingerprint(replace(EVENT, song_keys=("G",)), (SONG,))
    assert original == source_event_fingerprint(EVENT, (replace(SONG, key="A"),))
    assert source_event_fingerprint(replace(EVENT, song_keys=("C#",)), (SONG,)) == source_event_fingerprint(
        replace(EVENT, song_keys=("Db",)), (SONG,))
    assert source_event_fingerprint(replace(EVENT, song_keys=()), (SONG,)) != source_event_fingerprint(
        replace(EVENT, song_keys=()), (replace(SONG, key="G"),))


def test_repeated_song_occurrences_use_different_arrangements_and_stable_ownership_keys():
    event = replace(EVENT, song_ids=("s", "s", "s"), song_keys=("D", "G", "D"))
    target = target_provider((TargetSong("ct", "Song", "Artist", "123", (
        Arrangement("old", "Original", True, "C"),
        Arrangement("d", "Acoustic", False, "D"),
    )),))
    result = plan((event,), target=target)
    assert len(result.preparation_actions) == 1
    assert result.preparation_actions[0].payload["key"] == "G"
    actions = result.events[0].actions
    assert [action.payload["key"] for action in actions] == ["D", "G", "D"]
    assert [action.payload["source_key"] for action in actions] == ["main:0:s", "main:1:s", "main:2:s"]
    assert actions[0].payload["arrangement_id"] == actions[2].payload["arrangement_id"] == "d"
    assert target.songs["ct"].arrangements[0].key == "C"


def test_matching_arrangements_prefer_default_then_stable_id():
    arrangements = (Arrangement("z", "Z", False, "Db"), Arrangement("a", "A", True, "C#"))
    target = target_provider((TargetSong("ct", "Song", "Artist", "123", arrangements),))
    result = plan((replace(EVENT, song_keys=("C#",)),), target=target)
    assert result.preparation_actions == ()
    assert result.events[0].actions[0].payload["arrangement_id"] == "a"


def test_multiple_source_ids_matching_one_song_share_arrangement_creation():
    event = replace(EVENT, song_ids=("s", "alias"), song_keys=("D", "D"))
    songs = (SONG, replace(SONG, id="alias"))
    for existing in ((), (TargetSong("ct", "Song", "Artist", "123", (Arrangement("old", "Original", True, "C"),)),)):
        result = plan((event,), songs, target_provider(existing))
        assert sum(action.kind is ActionKind.CREATE_ARRANGEMENT for action in result.preparation_actions) == 1
        assert result.events[0].actions[0].payload["arrangement_key"] == result.events[0].actions[1].payload["arrangement_key"]


def test_missing_keys_keep_legacy_selection_and_invalid_keys_fail_before_preparation():
    target = target_provider((TargetSong("ct", "Song", "Artist", "123", (Arrangement("old", "Original", True, "C"),)),))
    result = plan((replace(EVENT, song_keys=()),), (replace(SONG, key=None),), target)
    assert not result.preparation_actions
    assert result.events[0].actions[0].payload["arrangement_id"] == "old"
    invalid = plan((replace(EVENT, song_keys=("nonsense",)),))
    assert invalid.events[0].status is EventPlanStatus.FAILED
    assert invalid.events[0].issues[0].code == "invalid_song_key"
    assert not invalid.preparation_actions


def test_transitive_preparation_survives_when_inline_arrangement_is_unused():
    result = plan()
    create, variant = result.preparation_actions
    assert create.kind is ActionKind.CREATE_SONG and create.payload["key"] == "C"
    assert variant.kind is ActionKind.CREATE_ARRANGEMENT and variant.payload["key"] == "D"
    assert variant.dependencies == (create.payload["song_resource_key"],)
    assert len(arrangement_name("X" * 50, "C#m")) == 50
    assert sync_plan_from_dict(result.as_dict()).fingerprint == result.fingerprint


@pytest.mark.parametrize("mode", list(SyncMode))
def test_key_only_change_syncs_and_repeated_runs_create_no_duplicate_arrangements(mode):
    profile = replace(PROFILE, sync_mode=mode)
    target = target_provider()
    source = FakeSourceProvider((EVENT,), (SONG,))
    previous = None
    for index, key in enumerate(("D", "G", "G", "D")):
        source.events = (replace(EVENT, song_keys=(key,)),)
        repository = MemoryRunRepository(RunSpecification(f"run-{index}", "w", "wt", "ct", profile))
        if previous:
            repository.ownership_rows = previous.ownership_rows
            repository.event_sync_rows = previous.event_sync_rows
        expected = RunStatus.SKIPPED if index == 2 and mode is SyncMode.SOURCE_CHANGES_ONLY else RunStatus.SUCCEEDED
        assert execute(repository, target, source) is expected
        assert target.agendas["event"].items[1].key == key
        previous = repository
    assert len(target.songs) == 1
    assert {arr.key for song in target.songs.values() for arr in song.arrangements} == {"C", "D", "G"}
    assert [kind for kind, _ in target.writes].count("create_song") == 1
    assert [kind for kind, _ in target.writes].count("create_arrangement") == 2


def test_dry_run_contains_keys_and_never_writes():
    target = target_provider()
    repository = MemoryRunRepository(RunSpecification("run", "w", "wt", "ct", PROFILE, dry_run=True))
    assert execute(repository, target, FakeSourceProvider((EVENT,), (SONG,))) is RunStatus.SUCCEEDED
    assert not target.writes
    assert repository.plan.events[0].actions[0].payload["key"] == "D"


def test_different_services_keep_their_keys_and_leave_historical_arrangements_untouched():
    original = Arrangement("original", "Original", True, "C")
    target = target_provider((TargetSong("ct", "Song", "Artist", "123", (original,)),))
    other_start = NOW + timedelta(days=1)
    target.events += (TargetEvent("other", "Other service", other_start),)
    target.agendas["other"] = Agenda("other", (AgendaItem("header", 0, "header", "Songs"),))
    history = Agenda("history", (AgendaItem("historic-song", 0, "song", song_id="ct", arrangement_id="original", key="C"),))
    target.agendas["history"] = history
    other = replace(EVENT, id="other-service", starts_at=(other_start,), song_keys=("G",))
    repository = MemoryRunRepository(RunSpecification("run", "w", "wt", "ct", PROFILE))
    assert execute(repository, target, FakeSourceProvider((EVENT, other), (SONG,))) is RunStatus.SUCCEEDED
    assert target.agendas["event"].items[1].key == "D"
    assert target.agendas["other"].items[1].key == "G"
    assert target.agendas["history"] == history
    assert target.songs["ct"].arrangements[0] == original
    assert sum(arr.is_default for arr in target.songs["ct"].arrangements) == 1


def test_invalid_key_fails_only_its_event_without_creating_unused_arrangements():
    target = target_provider()
    other_start = NOW + timedelta(days=1)
    target.events += (TargetEvent("other", "Other service", other_start),)
    target.agendas["other"] = Agenda("other", (AgendaItem("header", 0, "header", "Songs"),))
    other = replace(EVENT, id="other-service", starts_at=(other_start,), song_keys=("invalid",))
    repository = MemoryRunRepository(RunSpecification("run", "w", "wt", "ct", PROFILE))
    assert execute(repository, target, FakeSourceProvider((EVENT, other), (SONG,))) is RunStatus.PARTIAL
    assert target.agendas["event"].items[1].key == "D"
    assert len(target.agendas["other"].items) == 1
    assert {arr.key for song in target.songs.values() for arr in song.arrangements} == {"C", "D"}


def test_old_persisted_plan_without_key_fields_still_executes():
    target = target_provider()
    legacy = plan((replace(EVENT, song_keys=()),), (replace(SONG, key=None),), target).as_dict()
    for action in legacy["preparation_actions"] + legacy["events"][0]["actions"]:
        action["payload"].pop("key", None)
        action["payload"].pop("song_name", None)
    repository = MemoryRunRepository(RunSpecification("run", "w", "wt", "ct", PROFILE))
    repository.plan = sync_plan_from_dict(legacy)
    assert execute(repository, target, FakeSourceProvider((), ())) is RunStatus.SUCCEEDED


def test_arrangement_key_changed_after_planning_is_not_linked():
    song = TargetSong("ct", "Song", "Artist", "123", (Arrangement("arr", "Standard", True, "D"),))
    target = target_provider((song,))
    repository = MemoryRunRepository(RunSpecification("run", "w", "wt", "ct", PROFILE))
    repository.plan = plan(target=target)
    target.songs["ct"] = replace(song, arrangements=(replace(song.arrangements[0], key="G"),))
    assert execute(repository, target, FakeSourceProvider((), ())) is RunStatus.FAILED
    assert not target.writes


def test_crash_reconciliation_and_verification_require_expected_key():
    song = TargetSong("ct", "Song", "Artist", "123", (Arrangement("arr", "Standard", True, "D"),))
    target = target_provider((song,))
    result = plan(target=target)
    action = result.events[0].actions[0]
    committed = Agenda("event", (*target.agendas["event"].items, AgendaItem("new", 1, "song", song_id="ct", arrangement_id="arr", key="G")))
    assert _reconcile_agenda_effect(action, committed, {}) is None
    target.agendas["event"] = committed
    repository = MemoryRunRepository(RunSpecification("run", "w", "wt", "ct", PROFILE))
    repository.plan = result
    repository.executions[action.id] = ActionExecution(action.id, ActionStatus.APPLIED, {"agenda_item_id": "new"})
    assert execute(repository, target, FakeSourceProvider((), ())) is RunStatus.FAILED
    assert not target.writes
