"""Telegram payload helpers."""

from __future__ import annotations


def extract_message_text(message: dict) -> str:
    """Return text/caption stripped."""
    return (message.get("text") or message.get("caption") or "").strip()
