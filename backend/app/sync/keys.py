"""Musical key normalization shared by source parsing and target reconciliation."""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from .models import Arrangement, SourceEvent, SourceSong

_KEY = re.compile(r"^([A-Ga-g])([#b]?)(m?)$", re.IGNORECASE)
_PITCHES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
# All spellings below are accepted by ChurchTools' ArrangementKey enum.
_MAJOR = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
_MINOR = ("Cm", "C#m", "Dm", "D#m", "Em", "Fm", "F#m", "Gm", "G#m", "Am", "Bbm", "Bm")


def normalize_key(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(unicodedata.normalize("NFC", value).split()).replace("♯", "#").replace("♭", "b")
    match = _KEY.fullmatch(text)
    if match is None:
        return None
    pitch = (_PITCHES[match[1].upper()] + {"": 0, "#": 1, "b": -1}[match[2].lower()]) % 12
    return (_MINOR if match[3] else _MAJOR)[pitch]


def effective_song_key(event: SourceEvent, index: int, song: SourceSong) -> str | None:
    return normalize_key(event.song_keys[index] if event.song_keys else song.key)


def key_matches(actual: str | None, expected: str | None) -> bool:
    """An unspecified desired key preserves legacy, key-agnostic behavior."""
    if expected is None:
        return True
    normalized = normalize_key(expected)
    return normalized is not None and normalize_key(actual) == normalized


def select_arrangement(arrangements: Sequence[Arrangement], key: str | None = None) -> Arrangement | None:
    candidates = [item for item in arrangements if key_matches(item.key, key)]
    return min(candidates, key=lambda item: (not item.is_default, item.id), default=None)


def arrangement_name(base: str, key: str | None) -> str:
    suffix = f" ({key})" if key else ""
    return base[:50 - len(suffix)].rstrip() + suffix
