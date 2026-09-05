"""Telegram payload helpers."""

from __future__ import annotations

import re

_DIGITS_ONLY = re.compile(r"^[0-9]+$")


def extract_message_text(message: dict) -> str:
    """Return text/caption stripped."""
    return (message.get("text") or message.get("caption") or "").strip()


def parse_telegram_id_text(text: str) -> int | None:
    """Parse positive numeric Telegram user id from plain text."""
    stripped = text.strip()
    if not stripped or not _DIGITS_ONLY.match(stripped):
        return None
    value = int(stripped)
    if value <= 0:
        return None
    return value


def extract_forwarded_user_id(message: dict) -> int | None:
    """Extract sender user id from a forwarded message, if visible to the bot."""
    forward_from = message.get("forward_from")
    if isinstance(forward_from, dict):
        user_id = forward_from.get("id")
        if user_id is not None:
            return int(user_id)

    forward_origin = message.get("forward_origin")
    if not isinstance(forward_origin, dict):
        return None

    origin_type = forward_origin.get("type")
    if origin_type == "user":
        sender_user = forward_origin.get("sender_user")
        if isinstance(sender_user, dict) and sender_user.get("id") is not None:
            return int(sender_user["id"])
    return None
