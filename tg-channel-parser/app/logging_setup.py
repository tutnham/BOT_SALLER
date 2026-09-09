"""Structured logging with secret redaction (doc §14.2)."""

from __future__ import annotations

import logging
import sys
from typing import Any

from loguru import logger


class _SecretFilter:
    """Mask exact secret substrings before log write."""

    def __init__(self) -> None:
        self._secrets: list[str] = []

    def set_secrets(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def __call__(self, record: Any) -> bool:
        message = str(record["message"])
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, "***")
        record["message"] = message
        return True


_secret_filter = _SecretFilter()


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    """Configure loguru sink; redact secrets."""
    if secrets:
        _secret_filter.set_secrets(secrets)

    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            '{{"time":"{time:YYYY-MM-DDTHH:mm:ss.SSSZ}",'
            '"level":"{level}",'
            '"event":"{message}",'
            '"module":"{name}"}}'
        ),
        filter=lambda record: _secret_filter(record),
        enqueue=False,
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
