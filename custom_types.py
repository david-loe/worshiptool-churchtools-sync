from typing import TypedDict


class Config_Song_Placement(TypedDict):
    after: dict[str, str | int]
    songs: str


class Config_CT_Event(TypedDict):
    name: str
    song_placements: list[Config_Song_Placement]


class Config(TypedDict):
    ct_events: list[Config_CT_Event]
    ct_item_defaults: dict[str, any]
    ct_song_defaults: dict[str, any]


class CT_Event(TypedDict):
    startDate: str
    id: int
    name: str


class WT_Event(TypedDict):
    times: list[str]
    id: str
    songs: list[str]
    name: str | None


class WT_Song(TypedDict):
    id: str
    name: str
    artist: str
    ccli: str | None
    key: str


class CT_Song_Arrangement(TypedDict):
    id: int
    name: str
    isDefault: bool
    bpm: str
    keyOfArrangement: str


class CT_Song(TypedDict):
    id: int
    name: str
    ccli: str
    author: str
    arrangements: list[CT_Song_Arrangement]
    category: dict[str, str, int]
