import argparse
import logging
import os
import sys
import traceback
import yaml
from dotenv import load_dotenv
from manager import AgendaException, CT_Event_Manager, CT_Song_Manager
from custom_types import Config
from matcher import Event_Matcher, Song_Matcher
from telegram import send_telegram_message
from worshiptools_api import Worshiptools_API
from churchtools_api import Churchtools_API


def main():
    load_dotenv()
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

    # YAML-Konfigurationsdatei laden
    try:
        with open(args.config, "r", encoding="utf-8") as file:
            config: Config = yaml.safe_load(file)
    except Exception as e:
        logging.error(f"Fehler beim Laden der Konfigurationsdatei {args.config}: {e}")
        sys.exit(1)

    event_matcher = Event_Matcher(os.environ.get("WORSHIPTOOLS_TZ"), os.environ.get("CHURCHTOOLS_TZ"), config)
    ct_api = Churchtools_API(os.environ.get("CHURCHTOOLS_BASE_URL"), os.environ.get("CHURCHTOOLS_LOGIN_TOKEN"))
    wt_api = Worshiptools_API(
        os.environ.get("WORSHIPTOOLS_EMAIL"),
        os.environ.get("WORSHIPTOOLS_PASSWORD"),
        os.environ.get("WORSHIPTOOLS_ACCOUNT_ID"),
    )
    ct_songs = ct_api.get_all("songs", {"limit": 100})["data"]
    wt_songs = wt_api.get_all("song", {"rows": 100})["docs"]
    song_matcher = Song_Matcher(wt_songs, ct_songs)
    song_manager = CT_Song_Manager(ct_api, config, song_matcher)

    wt_services = wt_api.get("service")["docs"]
    ct_events = ct_api.get("events")["data"]
    events = event_matcher.match(wt_services, ct_events)
    for event in events:
        logging.info(
            f"Syncing to: {event['ct']['name']} ({event['ct']['startDate']}) - using config: {event['config']['name']}"
        )
        try:
            event_manager = CT_Event_Manager(ct_api, config, event["ct"]["id"])
            songs = song_manager.convert(event["wt"]["songs"])
            event_manager.place_songs(songs, event["config"]["song_placements"])
        except AgendaException as e:
            logging.warning(f"Unable to sync to: {event['ct']['name']} - {event['ct']['startDate']}: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Hier landen nur Fehler, die nicht bereits im main()-Code abgefangen wurden
        error_message = f"Unbehandelter Fehler in Skript:\n\n{traceback.format_exc()}"
        logging.critical(error_message, exc_info=True)
        send_telegram_message(error_message)
        sys.exit(1)
