import argparse
import logging
import os
import sys
import yaml
from dotenv import load_dotenv
from manager import AgendaException, CT_Event_Manager, CT_Song_Manager
from custom_types import Config
from matcher import Event_Matcher, Song_Matcher
from worshiptools_api import Worshiptools_API
from churchtools_api import Churchtools_API


def main():
    parser = argparse.ArgumentParser(description="Worshiptools ↔️ Churchtools Sync")
    parser.add_argument("--loglevel", default="INFO", help="Setzt das Loglevel (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur Konfigurationsdatei")
    args = parser.parse_args()

    # Loglevel einstellen
    log_level = getattr(logging, args.loglevel.upper(), None)
    if not isinstance(log_level, int):
        print(f"Ungültiges Loglevel: {args.loglevel}")
        sys.exit(1)

    logging.basicConfig(level=log_level)
    load_dotenv()

    # YAML-Konfigurationsdatei laden
    try:
        with open(args.config, "r", encoding="utf-8") as file:
            config: Config = yaml.safe_load(file)
    except Exception as e:
        logging.error(f"Fehler beim Laden der Konfigurationsdatei {args.config}: {e}")
        sys.exit(1)

    event_matcher = Event_Matcher(os.environ.get("WORSHIPTOOLS_TZ"), os.environ.get("CHURCHTOOLS_TZ"))
    ct_api = Churchtools_API(os.environ.get("CHURCHTOOLS_BASE_URL"), os.environ.get("CHURCHTOOLS_LOGIN_TOKEN"))
    wt_api = Worshiptools_API(
        os.environ.get("WORSHIPTOOLS_EMAIL"),
        os.environ.get("WORSHIPTOOLS_PASSWORD"),
        os.environ.get("WORSHIPTOOLS_ACCOUNT_ID"),
    )
    ct_songs = ct_api.get_all("songs", {"limit": 100})["data"]
    for song in ct_songs:
        if song["ccli"] == "":
            print(f"{song['id']} - {song['name']}")
    wt_songs = wt_api.get_all("song", {"rows": 100})["docs"]
    song_matcher = Song_Matcher(wt_songs, ct_songs)
    song_manager = CT_Song_Manager(ct_api, config, song_matcher)

    wt_services = wt_api.get("service")["docs"]
    ct_events = ct_api.get("events")["data"]
    events = event_matcher.match(wt_services, ct_events)
    for event in events:
        if event["wt"]["songs"]:
            ct_event_config = None
            for ct_event in config["ct_events"]:
                if ct_event["name"] in event["ct"]["name"]:
                    ct_event_config = ct_event
            if ct_event_config:
                logging.info(f"Syncing to: {event['ct']['name']} - {event['ct']['startDate']}")
                try:
                    event_manager = CT_Event_Manager(ct_api, config, event["ct"]["id"])
                    songs = song_manager.convert(event["wt"]["songs"])
                    event_manager.place_songs(songs, ct_event_config["song_placements"])
                except AgendaException as e:
                    logging.warning(
                        f"Unable to sync to: {event['ct']['name']} - {event['ct']['startDate']}\nError: {e}"
                    )


if __name__ == "__main__":
    main()
