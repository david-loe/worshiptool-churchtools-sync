from datetime import datetime
from typing import TypedDict, Union
from zoneinfo import ZoneInfo
from custom_types import CT_Event, CT_Song, WT_Event, WT_Song


class CT_Type(TypedDict):
    ct: CT_Event


class WT_Type(TypedDict):
    wt: WT_Event


class Event_Matcher:
    def __init__(self, wt_tz, ct_tz):
        self.ct_tzinfo = ZoneInfo(ct_tz)
        self.wt_tzinfo = ZoneInfo(wt_tz)

    def match(self, wt_events: list[WT_Event], ct_events: list[CT_Event]):
        matches: list[Union[CT_Type, WT_Type]] = []
        for ct_event in ct_events:
            ct_event_start = datetime.strptime(ct_event["startDate"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=self.ct_tzinfo
            )
            for wt_event in wt_events:
                for wt_event_time in wt_event["times"]:
                    wt_event_start = (
                        datetime.strptime(wt_event_time, "%Y-%m-%dT%H:%M:%S")
                        .replace(tzinfo=self.wt_tzinfo)
                        .astimezone(self.ct_tzinfo)
                    )
                    if wt_event_start == ct_event_start:
                        matches.append({"ct": ct_event, "wt": wt_event})
        return matches


class Song_Matcher:
    def __init__(self, wt_songs: list[WT_Song], ct_songs: list[CT_Song]):
        self.wt_songs = wt_songs
        self.ct_songs = ct_songs

    def match(self, wt_song_id: str):
        wt_song = self.find_wt_song({"id": wt_song_id})
        ct_song = None
        if wt_song:
            if wt_song["ccli"]:
                ct_song = self.find_ct_song({"ccli": wt_song["ccli"]})
            else:
                ct_song = self.find_ct_song({"name": wt_song["name"], "author": wt_song["artist"]})
            return ct_song

    def find_wt_song(self, filter: dict[str, any]):
        for wt_song in self.wt_songs:
            match = True
            for key, value in filter.items():
                if wt_song[key] != value:
                    match = False
                    break
            if match:
                return wt_song
        return None

    def find_ct_song(self, filter: dict[str, any]):
        for ct_song in self.ct_songs:
            match = True
            for key, value in filter.items():
                if ct_song[key] != value:
                    match = False
                    break
            if match:
                return ct_song
        return None

    def add_ct_song(self, new_ct_song: CT_Song):
        self.ct_songs.append(new_ct_song)
