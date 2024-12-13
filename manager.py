from churchtools_api import Churchtools_API
from custom_types import CT_Song, Config, Config_Song_Placement, WT_Song
from matcher import Song_Matcher
from utils import slice_list


class AgendaException(Exception):
    pass


class CT_Event_Manager:
    def __init__(self, ct_api: Churchtools_API, config: Config, ct_event_id: int):
        self.ct_api = ct_api
        self.config = config
        self.ct_event_id = ct_event_id
        res = self.ct_api.get(f"events/{self.ct_event_id}/agenda")
        if res:
            self.ct_agenda = res["data"]
        else:
            raise AgendaException(f"No Agenda found for event {self.ct_event_id}!")

    def place_songs(self, songs: list[CT_Song], song_placements: list[Config_Song_Placement]):
        sort_placements = []
        for song_placement in song_placements:
            position = self.find_song_placement(song_placement)
            placement_songs = slice_list(songs, song_placement["songs"])
            sort_placements.append({"position": position, "songs": placement_songs})
        placements = sorted(sort_placements, key=lambda p: p["position"])

        def place_placement(placement, position_correction: int):
            count_new = 0
            for i, song in enumerate(placement["songs"]):
                if self.place_song(song, placement["position"] + i + position_correction, position_correction):
                    count_new = count_new + 1
            return count_new

        position_correction = 0
        for placement in placements:
            position_correction = place_placement(placement, position_correction) + position_correction

    def place_song(self, ct_song: CT_Song, position: int, position_correction: int) -> bool:
        item = {
            "agenda_id": self.ct_agenda["id"],
            "arrangement_id": ct_song["arrangements"][0]["id"],
            "bezeichnung": "",
            "header_yn": "0",
            "responsible": "",
            "sortkey": position,
            "duration": 0,
            "event_ids": [self.ct_event_id],
        }
        item.update(self.config["ct_item_defaults"])
        new_item = (
            len(self.ct_agenda["items"]) <= position - position_correction
            or self.ct_agenda["items"][position - position_correction]["type"] != "song"
        )
        skip = False
        if new_item:
            self.shuffle_agenda_items(position)
        else:
            item["id"] = self.ct_agenda["items"][position - position_correction]["id"]
            skip = self.ct_agenda["items"][position - position_correction]["song"]["songId"] == ct_song["id"]

        if not skip:
            self.ct_api.save_item_ajax(item)
        return new_item

    def shuffle_agenda_items(self, new_item_position: int):
        items = self.ct_api.load_agenda_items_ajax(self.ct_agenda["id"])["data"]
        agenda = self.ct_api.load_agenda_ajax(self.ct_agenda["id"])["data"][str(self.ct_agenda["id"])]
        changed = False
        for id, item in items.items():
            if int(item["sortkey"]) >= new_item_position:
                changed = True
                item["sortkey"] = int(item["sortkey"]) + 1
        if changed:
            agenda["items"] = items
            self.ct_api.save_agenda_ajax(agenda)

    def find_song_placement(self, song_placement: Config_Song_Placement) -> int | None:
        """
        get position of the placement
        """
        for item in self.ct_agenda["items"]:
            match = True
            for key, value in song_placement["agenda_item"].items():
                if item[key] != value:
                    match = False
                    break
            if match:
                if song_placement["position"] == "after":
                    return item["position"] + 1
                elif song_placement["position"] == "at":
                    return item["position"]
                elif song_placement["position"] == "before":
                    return item["position"] - 1
        return None


class CT_Song_Manager:
    def __init__(self, ct_api: Churchtools_API, config: Config, song_matcher: Song_Matcher):
        self.ct_api = ct_api
        self.config = config
        self.song_matcher = song_matcher

    def convert(self, wt_song_ids: list[str]):
        ct_songs: list[CT_Song] = []
        for wt_song_id in wt_song_ids:
            ct_song = self.song_matcher.match(wt_song_id)
            if not ct_song:
                wt_song = self.song_matcher.find_wt_song({"id": wt_song_id})
                if wt_song:
                    ct_song = self.create_ct_song(wt_song)
            if ct_song:
                ct_songs.append(ct_song)
        return ct_songs

    def create_ct_song(self, wt_song: WT_Song | None) -> CT_Song:
        new_song_id = self.ct_api.create_song(
            title=wt_song["name"],
            songcategory_id=self.config["ct_song_defaults"]["songcategory_id"],
            author=wt_song["artist"],
            ccli=wt_song["ccli"],
            tonality=wt_song["key"],
        )
        new_song = self.ct_api.get(f"songs/{new_song_id}")["data"]
        self.song_matcher.add_ct_song(new_song)
        return new_song
