"""Stable hashes for deciding whether WorshipTools input needs reconciliation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .matching import normalize_ccli, normalize_text
from .models import ProfileConfig, SourceEvent, SourceSong, to_primitive


def source_event_fingerprint(
    event: SourceEvent, source_songs: Sequence[SourceSong]
) -> str:
    """Hash ordered song identity and matching metadata; deliberately ignore key/tempo."""

    candidates: dict[str, list[SourceSong]] = {}
    for song in source_songs:
        candidates.setdefault(song.id, []).append(song)
    ordered_songs = []
    for song_id in event.song_ids:
        metadata = sorted(
            (
                {
                    "name": normalize_text(song.name),
                    "artist": normalize_text(song.artist),
                    "ccli": normalize_ccli(song.ccli),
                }
                for song in candidates.get(song_id, ())
            ),
            key=_canonical_json,
        )
        ordered_songs.append({"id": song_id, "metadata": metadata})
    return _sha256({"songs": ordered_songs})


def sync_config_fingerprint(
    profile: ProfileConfig,
    *,
    source_connection_id: str,
    target_connection_id: str,
) -> str:
    """Hash only settings that can alter event matching or agenda output."""

    return _sha256(
        {
            "source_connection_id": source_connection_id,
            "target_connection_id": target_connection_id,
            "source_timezone": profile.source_timezone,
            "target_timezone": profile.target_timezone,
            "match_mode": profile.match_mode.value,
            "selectors": to_primitive(profile.selectors),
            "placements": to_primitive(profile.placements),
            "auto_create_songs": profile.auto_create_songs,
            "song_category_id": profile.song_category_id,
            "arrangement_name": profile.arrangement_name,
            "agenda_item_defaults": dict(profile.agenda_item_defaults),
        }
    )


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
