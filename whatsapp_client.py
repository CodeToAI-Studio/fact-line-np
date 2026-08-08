"""
whatsapp_client.py

Shared WhatsApp Cloud API client. Used by:
  - whatsapp_bot.py: sends the initial approval-request template message
  - main.py: sends a plain-text confirmation reply after a button tap

Kept in one place so the actual request format only needs to be correct
in one spot, not duplicated.
"""

import os
import re

import requests

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

_API_URL = (
    f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    if WHATSAPP_PHONE_NUMBER_ID else None
)
_HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


_WHITESPACE_RUN = re.compile(r"\s+")


def _sanitize_param(value) -> str:
    """WhatsApp rejects template body parameters that contain newlines, tabs
    or 4+ consecutive spaces (error 132000, "Parameter format does not match
    format in the created template"), and rejects empty ones outright. Our
    social_summary is a multi-line 2-4 line caption and is an empty string
    when a story wasn't corroborated, so both cases are live -- collapse all
    whitespace to single spaces and substitute a placeholder when blank."""
    text = _WHITESPACE_RUN.sub(" ", str(value if value is not None else "")).strip()
    return text or "(no summary)"


def send_template_message(to_number: str, template_name: str, body_params: list, button_payloads: list):
    """Sends a pre-approved template message with quick-reply buttons.
    button_payloads is a list of strings, one per button, in order --
    e.g. [f"approve:{post.id}", f"reject:{post.id}"]. Returns the sent
    message's WhatsApp message ID, or None if the send failed."""
    components = [{
        "type": "body",
        "parameters": [{"type": "text", "text": _sanitize_param(p)} for p in body_params],
    }]
    for i, payload in enumerate(button_payloads):
        components.append({
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(i),
            "parameters": [{"type": "payload", "payload": payload}],
        })

    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "en_US"},
            "components": components,
        },
    }

    response = requests.post(_API_URL, json=data, headers=_HEADERS, timeout=15)
    if response.status_code != 200:
        print(f"WhatsApp template send failed: {response.status_code} {response.text}")
        return None
    return response.json().get("messages", [{}])[0].get("id")


def send_text_message(to_number: str, text: str) -> bool:
    """Sends a free-form text message. Only works within 24 hours of the
    recipient's last message to you (a WhatsApp session-message rule) --
    fine for replying to a button tap, since that IS the recipient
    messaging you."""
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }
    response = requests.post(_API_URL, json=data, headers=_HEADERS, timeout=15)
    if response.status_code != 200:
        print(f"WhatsApp text send failed: {response.status_code} {response.text}")
        return False
    return True
