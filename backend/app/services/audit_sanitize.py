"""Allowlisted audit payloads — no secrets or message bodies."""

from __future__ import annotations

from typing import Any

_SENSITIVE_KEYS = frozenset(
    {
        "telegram_bot_token",
        "webhook_secret",
        "api_key",
        "session_string",
        "password",
        "token",
        "raw_text",
        "text",
        "payload",
    }
)


def sanitize_audit_state(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        lower = key.lower()
        if lower in _SENSITIVE_KEYS or "token" in lower or "secret" in lower:
            continue
        if isinstance(value, dict):
            nested = sanitize_audit_state(value)
            if nested:
                cleaned[key] = nested
        elif isinstance(value, list):
            cleaned[key] = [str(item)[:120] for item in value[:20]]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 500:
                cleaned[key] = value[:500]
            else:
                cleaned[key] = value
        else:
            cleaned[key] = str(value)[:200]
    return cleaned or None
