import pytest

from manager import AgendaException, CT_Event_Manager, CT_Song_Manager


class FakeAgendaApi:
    def __init__(self):
        self.saved_items = []
        self.saved_agendas = []
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

    def load_agenda_items_ajax(self, agenda_id):
        return {
            "data": {
                "11": {"id": 11, "sortkey": 0},
                "12": {"id": 12, "sortkey": 1},
                "13": {"id": 13, "sortkey": 2},
            }
        }

    def load_agenda_ajax(self, agenda_id):
        return {"data": {str(agenda_id): {"id": agenda_id}}}

    def save_agenda_ajax(self, agenda):
        self.saved_agendas.append(agenda)

    def save_item_ajax(self, item):
        self.saved_items.append(item)


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
    assert api.saved_items == []


def test_new_song_insertion_shuffles_and_saves_item():
    api = FakeAgendaApi()
    event_manager = manager(api)

    is_new = event_manager.place_song(ct_song(song_id=8), position=2, position_correction=0)

    assert is_new is True
    assert api.saved_agendas
    assert api.saved_agendas[0]["items"]["13"]["sortkey"] == 3
    assert api.saved_items[0]["sortkey"] == 2


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
