import pytest

from manager import AgendaException, CT_Event_Manager, CT_Song_Manager


class FakeAgendaApi:
    def __init__(self):
        self.created_items = []
        self.updated_items = []
        self.next_item_id = 100
        self.agenda = {
            "id": 1,
            "items": [
                {"id": 11, "position": 0, "sortkey": 0, "title": "Start", "type": "header"},
                {"id": 12, "position": 1, "sortkey": 1, "title": "Old", "type": "song", "song": {"songId": 7}},
                {"id": 13, "position": 2, "sortkey": 2, "title": "End", "type": "header"},
            ],
        }

    def get(self, endpoint):
        assert endpoint == "events/99/agenda"
        return {"data": self.agenda}

    def create_agenda_item(self, event_id, item, before_id=None, after_id=None):
        assert event_id == 99
        item_id = self.next_item_id
        self.next_item_id = self.next_item_id + 1
        self.created_items.append({"item": item, "before_id": before_id, "after_id": after_id})
        return {"data": {"id": item_id, **item}}

    def update_agenda_item(self, event_id, item_id, item, before_id=None, after_id=None):
        assert event_id == 99
        self.updated_items.append({"item_id": item_id, "item": item, "before_id": before_id, "after_id": after_id})
        return {"data": {"id": item_id, **item}}


def ct_song(song_id=1, arrangement_id=10):
    return {"id": song_id, "name": "Song", "ccli": "123", "author": "A", "arrangements": [{"id": arrangement_id}], "category": {}}


def manager(api=None):
    return CT_Event_Manager(api or FakeAgendaApi(), {"ct_item_defaults": {}, "ct_song_defaults": {}, "ct_events": []}, 99)


def test_missing_agenda_placement_raises_agenda_exception():
    event_manager = manager()

    with pytest.raises(AgendaException):
        event_manager.find_song_placement({"agenda_item": {"title": "Missing"}, "position": "after", "songs": "[:]"})


def test_existing_matching_song_item_is_skipped():
    api = FakeAgendaApi()
    event_manager = manager(api)

    is_new = event_manager.place_song(ct_song(song_id=7), position=1, position_correction=0)

    assert is_new is False
    assert api.created_items == []
    assert api.updated_items == []


def test_existing_different_song_item_is_updated():
    api = FakeAgendaApi()
    event_manager = manager(api)

    is_new = event_manager.place_song(ct_song(song_id=8), position=1, position_correction=0)

    assert is_new is False
    assert api.created_items == []
    assert api.updated_items == [
        {
            "item_id": 12,
            "item": {"type": "song", "title": "", "responsible": "", "duration": 0, "arrangementId": 10},
            "before_id": None,
            "after_id": None,
        }
    ]
    assert event_manager.ct_agenda["items"][1]["song"]["songId"] == 8


def test_new_song_insertion_uses_before_id():
    api = FakeAgendaApi()
    event_manager = manager(api)

    is_new = event_manager.place_song(ct_song(song_id=8), position=2, position_correction=0)

    assert is_new is True
    assert api.created_items == [
        {
            "item": {"type": "song", "title": "", "responsible": "", "duration": 0, "arrangementId": 10},
            "before_id": 13,
            "after_id": None,
        }
    ]
    assert event_manager.ct_agenda["items"][2]["song"]["songId"] == 8
    assert event_manager.ct_agenda["items"][3]["id"] == 13
    assert event_manager.ct_agenda["items"][3]["position"] == 3


def test_new_song_insertion_at_end_uses_after_id():
    api = FakeAgendaApi()
    event_manager = manager(api)

    is_new = event_manager.place_song(ct_song(song_id=8), position=3, position_correction=0)

    assert is_new is True
    assert api.created_items[0]["before_id"] is None
    assert api.created_items[0]["after_id"] == 13
    assert event_manager.ct_agenda["items"][3]["song"]["songId"] == 8


def test_multiple_inserted_songs_use_updated_local_agenda_order():
    api = FakeAgendaApi()
    event_manager = manager(api)

    event_manager.place_songs(
        [ct_song(song_id=8), ct_song(song_id=9, arrangement_id=11)],
        [{"agenda_item": {"title": "End"}, "position": "after", "songs": "[:]"}],
    )

    assert [call["after_id"] for call in api.created_items] == [13, 100]
    assert [item["song"]["songId"] for item in event_manager.ct_agenda["items"][3:5]] == [8, 9]


def test_invalid_song_slice_is_wrapped_as_agenda_exception():
    event_manager = manager()

    with pytest.raises(AgendaException):
        event_manager.place_songs([ct_song()], [{"agenda_item": {"title": "Start"}, "position": "after", "songs": "[abc]"}])


def test_song_manager_does_not_cache_failed_song_creation():
    class Api:
        def create_song(self, **kwargs):
            return None

    class Matcher:
        added = []

        def add_ct_song(self, song):
            self.added.append(song)

    matcher = Matcher()
    song_manager = CT_Song_Manager(Api(), {"ct_song_defaults": {"songcategory_id": 1}}, matcher)

    assert song_manager.create_ct_song({"name": "Song", "artist": "Artist", "ccli": None}) is None
    assert matcher.added == []
