"""
notifications.py
-----------------
The notification layer used to tell a customer their refund went
through. Follows the pattern you gave, made safe-by-default:

  - push() always prints to console first (so the demo works with zero
    config), and only actually calls Pushover if credentials are set.
  - send_email() is a placeholder -- wire it up to SES/SendGrid/SMTP
    later. For now it just logs what WOULD have been sent.
  - send_message() picks one or the other based on USE_EMAIL.

Nothing here ever receives a full account number -- callers (see
refund_processor.py) are only ever given the masked last-4 form.
"""
from __future__ import annotations
import os

USE_EMAIL = os.getenv("USE_EMAIL", "false").lower() == "true"

PUSHOVER_USER = os.getenv("PUSHOVER_USER", "")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN", "")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

EMAIL_PLACEHOLDER_ADDRESS = os.getenv("NOTIFICATION_EMAIL", "customer@example.com")  # placeholder only


def push(message: str) -> None:
    """Pushover notification. Always prints to console (acts as the
    'notification' in demo/placeholder mode); only hits the real
    Pushover API if PUSHOVER_USER/PUSHOVER_TOKEN are configured."""
    print(f"Push: {message}")

    if not (PUSHOVER_USER and PUSHOVER_TOKEN):
        return  # no real credentials configured -- console print above IS the notification

    try:
        import requests
        payload = {"user": PUSHOVER_USER, "token": PUSHOVER_TOKEN, "message": message}
        requests.post(PUSHOVER_URL, data=payload, timeout=5)
    except Exception as e:
        # Notification failures should never break the refund flow itself
        print(f"(Pushover request failed, non-fatal: {e})")


def send_email(subject: str, text_body: str, html_body: str) -> None:
    """PLACEHOLDER. Swap this out for a real provider (SES, SendGrid,
    SMTP...) when you have one. For now, logs what would have been sent."""
    print(
        f"[EMAIL PLACEHOLDER] To: {EMAIL_PLACEHOLDER_ADDRESS} | Subject: {subject}\n"
        f"{text_body}"
    )


def send_message(subject: str, text_body: str, html_body: str = "") -> None:
    if USE_EMAIL:
        send_email(subject, text_body, html_body)
    else:
        push(f"Subject:{subject}, text_body:{text_body}")
