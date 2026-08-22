from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.routers.profiles import (
    _agenda_defaults_for_storage,
    _merge_agenda_defaults,
)
from app.schemas import AgendaItemDefaults, ProfileCreate, ProfileUpdate


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": "value"},
        {"title": "x" * 101},
        {"note": "x" * 4001},
        {"responsible": "x" * 1001},
        {"duration": -1},
        {"duration": 86_401},
        {"duration": "300"},
        {"duration": 1.5},
        {"duration": True},
    ],
)
def test_agenda_item_defaults_reject_unknown_invalid_and_coerced_values(payload):
    with pytest.raises(ValidationError):
        AgendaItemDefaults.model_validate(payload)


def test_profile_schemas_expose_typed_agenda_item_defaults():
    profile = ProfileCreate(
        source_connection_id=uuid.uuid4(),
        target_connection_id=uuid.uuid4(),
        song_category_id=7,
        name="Agenda defaults",
        agenda_item_defaults={
            "title": "Lobpreis",
            "note": "Bitte direkt beginnen",
            "responsible": "[Worship Leader]",
            "duration": 300,
        },
    )
    patch = ProfileUpdate(agenda_item_defaults={"duration": 600})

    assert isinstance(profile.agenda_item_defaults, AgendaItemDefaults)
    assert profile.agenda_item_defaults.duration == 300
    assert isinstance(patch.agenda_item_defaults, AgendaItemDefaults)
    assert patch.agenda_item_defaults.model_dump(exclude_unset=True) == {
        "duration": 600
    }


def test_agenda_item_defaults_partial_patch_merges_and_null_clears():
    current = {
        "title": "Bisheriger Titel",
        "note": "Bleibt erhalten",
        "responsible": "Team",
        "duration": 300,
        "legacy_unknown": "wird nicht weitergeführt",
    }
    patch = AgendaItemDefaults(title=None, responsible="Leitung", duration=600)

    assert _merge_agenda_defaults(current, patch) == {
        "note": "Bleibt erhalten",
        "responsible": "Leitung",
        "duration": 600,
    }
    assert _merge_agenda_defaults(current, None) == {}
    assert _agenda_defaults_for_storage(
        AgendaItemDefaults(title=None, note="Hinweis", duration=None)
    ) == {"note": "Hinweis"}
