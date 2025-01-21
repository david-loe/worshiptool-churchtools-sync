import logging
import requests
import os


def send_telegram_message(message: str):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logging.info("Telegram Credentials nicht gesetzt.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
    }

    response = requests.post(url, data=payload)
    if response.status_code != 200:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht: {response.text}")
