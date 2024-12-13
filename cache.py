from datetime import datetime, timezone
import hashlib
import json
import yaml
from typing import Any, Dict

from custom_types import Config_CT_Event
from matcher import Event_Config_Match


class YamlDatabase:
    def __init__(self, file_path: str):
        self.file_path = file_path

    def _load_data(self) -> Dict[str, Any]:
        """Lädt Daten aus der YAML-Datei."""
        try:
            with open(self.file_path, "r") as file:
                return yaml.safe_load(file) or {}  # Lädt Daten oder gibt ein leeres Dict zurück
        except FileNotFoundError:
            return {}  # Datei existiert noch nicht, gib leeres Dict zurück

    def _save_data(self, data: Dict[str, Any]) -> None:
        """Speichert Daten in der YAML-Datei."""
        with open(self.file_path, "w") as file:
            yaml.safe_dump(data, file)

    def insert(self, key: str, value: Any) -> None:
        """Fügt ein neues Element hinzu oder aktualisiert ein bestehendes."""
        data = self._load_data()
        data[key] = value
        self._save_data(data)

    def get(self, key: str) -> Any:
        """Ruft ein Element anhand des Schlüssels ab."""
        data = self._load_data()
        return data.get(key, None)

    def delete(self, key: str) -> None:
        """Löscht ein Element anhand des Schlüssels."""
        data = self._load_data()
        if key in data:
            del data[key]
            self._save_data(data)


class Cache_Entry:
    event_datetime: str
    last_sync: str
    hash: str


class Cacher:
    cache_key = "cache"

    def __init__(self, db: YamlDatabase):
        self.db = db
        if not self.db.get(self.cache_key):
            self.db.insert(self.cache_key, [])
        self._clean_cache()

    def is_already_synced(self, event_config_match: Event_Config_Match):
        entries: list[Cache_Entry] = self.db.get(self.cache_key)
        current_hash = self._create_hash(event_config_match)

        # Prüfe ob es einen Eintrag mit gleichem Hash gibt
        for entry in entries:
            if entry["hash"] == current_hash:
                return True
        return False

    def cache_sync(self, event_config_match: Event_Config_Match):
        entries: list[Cache_Entry] = self.db.get(self.cache_key)
        entries.append(self._event_config_match_to_cache(event_config_match))
        self.db.insert(self.cache_key, entries)

    def _clean_cache(self):
        entries: list[Cache_Entry] = self.db.get(self.cache_key)

        valid_entries = [
            entry for entry in entries if datetime.fromisoformat(entry["event_datetime"]) >= datetime.now(timezone.utc)
        ]

        self.db.insert(self.cache_key, valid_entries)

    def _event_config_match_to_cache(self, event_config_match: Event_Config_Match):
        cache_entry: Cache_Entry = {
            "event_datetime": event_config_match["time"].isoformat(),
            "hash": self._create_hash(event_config_match),
            "last_sync": datetime.now(),
        }
        return cache_entry

    def _create_hash(self, event_config_match: Event_Config_Match) -> str:
        wt_event_id = event_config_match["wt"]["id"]
        ct_event_id = event_config_match["ct"]["id"]
        wt_songs = event_config_match["wt"]["songs"]
        config = event_config_match["config"]
        hash_input = {"wt_event_id": wt_event_id, "ct_event_id": ct_event_id, "wt_songs": wt_songs, "config": config}
        json_data = json.dumps(hash_input, sort_keys=True).encode("utf-8")
        return hashlib.sha256(json_data).hexdigest()
