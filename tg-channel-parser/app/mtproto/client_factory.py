"""Pyrogram Client factory from session_string (in-memory)."""

from __future__ import annotations

from pyrogram import Client

from app.config import Settings, get_settings


def create_client(
    name: str = "listener",
    *,
    settings: Settings | None = None,
    session_string: str | None = None,
    in_memory: bool = True,
) -> Client:
    """Build Pyrogram Client. Uses TELEGRAM_SESSION_STRING when provided."""
    cfg = settings or get_settings()
    api_id = cfg.telegram_api_id
    api_hash = cfg.telegram_api_hash
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH required")

    kwargs: dict = {
        "name": name,
        "api_id": api_id,
        "api_hash": api_hash,
        "in_memory": in_memory,
    }
    sess = session_string if session_string is not None else cfg.telegram_session_string
    if sess:
        kwargs["session_string"] = sess
    return Client(**kwargs)


def create_auth_client(settings: Settings | None = None) -> Client:
    """In-memory client for interactive auth_cli (no session yet)."""
    cfg = settings or get_settings()
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
    return Client(
        name=":memory:",
        api_id=cfg.telegram_api_id,
        api_hash=cfg.telegram_api_hash,
        phone_number=cfg.telegram_phone_number,
        in_memory=True,
    )
