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
            try:
                placement_songs = slice_list(songs, song_placement["songs"])
            except ValueError as e:
                raise AgendaException(str(e)) from e
            sort_placements.append({"position": position, "songs": placement_songs})
        placements = sorted(sort_placements, key=lambda p: p["position"])

        def place_placement(placement, position_correction: int):
            count_new = 0
            for i, song in enumerate(placement["songs"]):
                if self.place_song(song, placement["position"] + i + position_correction):
                    count_new = count_new + 1
            return count_new

        position_correction = 0
        for placement in placements:
            position_correction = place_placement(placement, position_correction) + position_correction

    def place_song(self, ct_song: CT_Song, position: int, position_correction: int = 0) -> bool:
        items = self.ct_agenda.setdefault("items", [])
        target_position = max(0, position)
        target_item = items[target_position] if target_position < len(items) else None
        payload = self.build_song_item_payload(ct_song)

        if target_item and target_item["type"] == "song":
            if target_item.get("song", {}).get("songId") == ct_song["id"]:
                return False
            response = self.ct_api.update_agenda_item(self.ct_event_id, target_item["id"], payload)
            if response:
                items[target_position] = self.build_local_agenda_item(response, payload, ct_song, target_item)
                self.update_agenda_positions()
            return False

        before_id = target_item["id"] if target_item else None
        after_id = None if before_id or not items else items[-1]["id"]
        response = self.ct_api.create_agenda_item(self.ct_event_id, payload, before_id=before_id, after_id=after_id)
        if not response:
            return False

        items.insert(target_position, self.build_local_agenda_item(response, payload, ct_song))
        self.update_agenda_positions()
        return True

    def build_song_item_payload(self, ct_song: CT_Song) -> dict:
        item = {
            "type": "song",
            "title": "",
            "responsible": "",
            "duration": 0,
            "arrangementId": ct_song["arrangements"][0]["id"],
        }
        defaults = self.config.get("ct_item_defaults", {})
        if "bezeichnung" in defaults and "title" not in defaults:
            item["title"] = defaults["bezeichnung"]
        for key in ("title", "note", "responsible", "duration"):
            if key in defaults:
                item[key] = defaults[key]
        return item

    def build_local_agenda_item(self, response: dict, payload: dict, ct_song: CT_Song, current_item: dict | None = None):
        item = dict(current_item or {})
        response_item = response.get("data") if response else None
        if isinstance(response_item, dict):
            item.update(response_item)
        for key, value in payload.items():
            item.setdefault(key, value)
        item["type"] = "song"
        item["song"] = dict(item.get("song", {}))
        item["song"]["songId"] = ct_song["id"]
        return item

    def update_agenda_positions(self):
        for position, item in enumerate(self.ct_agenda["items"]):
            item["position"] = position

    def find_song_placement(self, song_placement: Config_Song_Placement) -> int:
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
        raise AgendaException(f"No item in agenda found matching {song_placement['agenda_item']}")


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
        new_song = self.ct_api.create_song(
            name=wt_song["name"],
            categoryId=self.config["ct_song_defaults"]["songcategory_id"],
            author=wt_song["artist"],
            ccli=wt_song["ccli"],
        )
        if new_song:
            self.song_matcher.add_ct_song(new_song)
        return new_song
