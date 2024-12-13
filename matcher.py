from datetime import datetime
import re
from typing import TypedDict, Union
from zoneinfo import ZoneInfo
from custom_types import CT_Event, CT_Song, Config, Config_CT_Event, WT_Event, WT_Song
from utils import parse_datetime


class Event_Match(TypedDict):
    ct: CT_Event
    wt: WT_Event


class Event_Config_Match(Event_Match):
    config: Config_CT_Event


class Event_Matcher:
    wt_time_formats = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"]

    def __init__(self, wt_tz, ct_tz, config: Config):
        self.ct_tzinfo = ZoneInfo(ct_tz)
        self.wt_tzinfo = ZoneInfo(wt_tz)
        self.config = config

    def match_by_time(self, wt_events: list[WT_Event], ct_events: list[CT_Event]):
        matches: list[Event_Match] = []
        for ct_event in ct_events:
            ct_event_start = datetime.strptime(ct_event["startDate"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=self.ct_tzinfo
            )
            for wt_event in wt_events:
                for wt_event_time in wt_event["times"]:
                    wt_event_start = (
                        parse_datetime(wt_event_time, self.wt_time_formats)
                        .replace(tzinfo=self.wt_tzinfo)
                        .astimezone(self.ct_tzinfo)
                    )
                    if wt_event_start == ct_event_start:
                        matches.append({"ct": ct_event, "wt": wt_event})
        return matches

    def match(self, wt_events: list[WT_Event], ct_events: list[CT_Event]):
        matches: list[Union[Event_Config_Match]] = []
        events = self.match_by_time(wt_events, ct_events)
        for event in events:
            if event["wt"]["songs"]:
                ct_event_config = None
                for ct_event in self.config["ct_events"]:
                    if "regex" in ct_event and ct_event["regex"]:
                        regex = re.compile(ct_event["regex"])
                        if regex.search(event["ct"]["name"]):
                            ct_event_config = ct_event
                            break
                    else:
                        if ct_event["name"] in event["ct"]["name"]:
                            ct_event_config = ct_event
                            break
                if ct_event_config:
                    matches.append({"ct": event["ct"], "wt": event["wt"], "config": ct_event_config})
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
