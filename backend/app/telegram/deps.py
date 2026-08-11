"""FastAPI dependencies for Telegram webhook routes (TECH DOC §7, §13)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.config import get_settings


def _secrets_match(provided: str | None, expected: str) -> bool:
    """Constant-time compare; never raise on non-ASCII header values."""
    if not provided:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8"),
    )


async def verify_webhook_secret(
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
) -> None:
    """
    Require ``X-Webhook-Secret`` == ``WEBHOOK_SECRET``.

    Mismatch or missing header → 401 without processing the body.
    """
    expected = get_settings().webhook_secret
    if not _secrets_match(x_webhook_secret, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_webhook_secret",
        )


async def verify_telegram_secret_token(
    x_telegram_secret_token: Annotated[
        str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
    ] = None,
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
) -> None:
    """
    Require Telegram secret token on the webhook endpoint.

    Also accepts legacy ``X-Webhook-Secret`` so n8n can keep forwarding during
    the cutover window before ``setWebhook`` points at backend directly.
    """
    settings = get_settings()
    if _secrets_match(
        x_telegram_secret_token,
        settings.telegram_webhook_secret_token,
    ):
        return
    if _secrets_match(x_webhook_secret, settings.webhook_secret):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid_telegram_secret_token",
    )
