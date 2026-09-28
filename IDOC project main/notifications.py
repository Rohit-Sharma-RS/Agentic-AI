"""notifications.py -- notification layer for ops alerts."""
import os

import requests

USE_EMAIL = os.getenv("USE_EMAIL", "false").lower() == "true"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_USER = os.getenv("PUSHOVER_USER")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN")


def push(message: str) -> None:
    print(f"Push: {message}")
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        print("[PUSHOVER] PUSHOVER_USER/PUSHOVER_TOKEN are not configured")
        return

    try:
        response = requests.post(
            PUSHOVER_URL,
            data={
                "user": PUSHOVER_USER,
                "token": PUSHOVER_TOKEN,
                "message": message,
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"[PUSHOVER] notification failed: {exc}")


def send_email(subject: str, text_body: str) -> None:
    print(f"[EMAIL PLACEHOLDER] Subject: {subject}\n{text_body}")


def send_message(subject: str, text_body: str) -> None:
    if USE_EMAIL:
        send_email(subject, text_body)
    else:
        print(f"[NOTIFICATION] Subject: {subject}\n{text_body}")


def send_human_notification(subject: str, text_body: str) -> None:
    """Send a phone alert whenever an operator must resolve an IDoc."""
    push(f"{subject}\n{text_body}")
    if USE_EMAIL:
        send_email(subject, text_body)
