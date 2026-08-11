"""Helpers for safe Telegram HTML output."""

from __future__ import annotations

from html import escape


def sanitize_telegram_html(text: str) -> str:
    """
    Conservative sanitizer for Telegram parse_mode HTML.

    Escapes user-controlled fragments to prevent tag injection.
    """
    return escape(text or "", quote=False)
