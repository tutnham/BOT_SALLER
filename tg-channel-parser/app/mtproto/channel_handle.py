"""Normalize a channel handle from the admin bot into username or numeric id."""

from __future__ import annotations

import re

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class InvalidChannelHandle(ValueError):
    """Handle is empty or neither a @username nor a numeric chat id."""


def split_channel_handle(handle: str) -> tuple[str | None, int | None]:
    """Return (username, telegram_channel_id). Exactly one of them is set."""
    raw = handle.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :].split("/", 1)[0]
            break
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.strip()
    if not raw:
        raise InvalidChannelHandle("empty_handle")
    if raw.lstrip("-").isdigit():
        value = int(raw)
        if value == 0:
            raise InvalidChannelHandle("invalid_channel_id")
        return None, value
    if _USERNAME_RE.fullmatch(raw) is None:
        raise InvalidChannelHandle("invalid_handle")
    return raw, None


def mtproto_chat_ref(channel_id: int | None, username: str | None, title: str | None) -> int | str | None:
    """Value Pyrogram get_chat accepts. Numeric ids must be int, not a string."""
    if channel_id is not None:
        return int(channel_id)
    if username:
        return username
    stored = (title or "").strip().lstrip("@")
    if stored.lstrip("-").isdigit():
        return int(stored)
    return stored or None
