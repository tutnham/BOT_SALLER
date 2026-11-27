"""Reusable job runners for cron and manual /jobs/* endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MarkupRule, Owner, PriceListDraft
from app.services.billing_service import (
    build_llm_billing_reminder_text,
    period_key_for_datetime,
    record_reminder_sent,
    release_reminder,
    reserve_reminder,
)
from app.services.price_service import (
    build_full_history_price,
    build_morning_price,
    format_draft_lines,
    format_review_sources_lines,
    notify_morning_price_draft,
    notify_unknown_markup_rules,
    resolve_price_draft_destinations,
)
from app.services.recheck_service import send_due_rechecks
from app.services.report_service import build_report
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.utils.html import TELEGRAM_HTML_PARSE_MODE


async def _resolve_owner_destinations(
    session: AsyncSession,
    *,
    fallback_chat_id: int | None = None,
) -> list[int]:
    """Return owners with ``dm_ok=true`` first, then a single fallback chat id."""
    result = await session.execute(
        select(Owner.telegram_id).where(Owner.dm_ok.is_(True))
    )
    owner_chat_ids = [int(chat_id) for chat_id in result.scalars().all()]
    if owner_chat_ids:
        return owner_chat_ids

    if fallback_chat_id is None:
        return []
    return [int(fallback_chat_id)]


async def run_morning_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    full_history: bool = False,
) -> dict[str, Any]:
    builder = build_full_history_price if full_history else build_morning_price
    result = await builder(session, telegram)
    prompt_ids = [int(rule_id) for rule_id in result.pop("markup_prompt_rule_ids", [])]
    notify_draft = bool(result.pop("notify_draft", False))
    draft_id = result.get("draft_id")
    await session.commit()
    if notify_draft and draft_id is not None:
        draft = await session.get(PriceListDraft, int(draft_id))
        items = draft.items if draft is not None and isinstance(draft.items, list) else []
        review = draft.review_sources if draft is not None else None
        await notify_morning_price_draft(
            session,
            telegram,
            draft_id=int(draft_id),
            items_block=format_draft_lines(items),
            review_block=format_review_sources_lines(review) if review else None,
        )
    if prompt_ids:
        rules: list[MarkupRule] = []
        for rule_id in prompt_ids:
            rule = await session.get(MarkupRule, rule_id)
            if rule is not None:
                rules.append(rule)
        destinations = await resolve_price_draft_destinations(session)
        if not destinations:
            destinations = await _resolve_owner_destinations(
                session,
                fallback_chat_id=get_settings().admin_alert_chat_id,
            )
        if destinations and rules:
            await notify_unknown_markup_rules(
                telegram,
                chat_ids=destinations,
                rules=rules,
            )
    return result


async def run_recheck_due(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    result = await send_due_rechecks(session, telegram)
    await session.commit()
    return result


async def run_daily_report(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    period: Literal["day", "week"],
) -> dict[str, int | str]:
    report_text = await build_report(session, period=period)
    await session.commit()

    settings = get_settings()
    destinations = await _resolve_owner_destinations(
        session, fallback_chat_id=settings.analytics_chat_id
    )
    if not destinations:
        logger.warning(
            "Daily report period={} built but no destinations "
            "(owners with dm_ok or ANALYTICS_CHAT_ID)",
            period,
        )
        return {
            "status": "ok",
            "period": period,
            "sent": 0,
            "failed": 0,
        }

    sent = 0
    failed = 0
    for chat_id in destinations:
        try:
            await telegram.send_message(
                chat_id,
                report_text,
                parse_mode=TELEGRAM_HTML_PARSE_MODE,
            )
            sent += 1
        except TelegramSendError:
            failed += 1

    if failed > 0:
        logger.warning(
            "Daily report period={} partial send failure sent={} failed={}",
            period,
            sent,
            failed,
        )

    return {
        "status": "ok",
        "period": period,
        "sent": sent,
        "failed": failed,
    }


async def run_llm_billing_reminder(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    """Send monthly LLM top-up reminder. Static text only — no LLM is invoked."""
    settings = get_settings()

    reminder_text = await build_llm_billing_reminder_text(session, settings)
    if reminder_text is None:
        return {
            "status": "skipped",
            "reason": "payment_url_unset",
        }

    fallback_chat_id = (
        settings.llm_billing_reminder_chat_id or settings.admin_alert_chat_id
    )
    destinations = await _resolve_owner_destinations(
        session, fallback_chat_id=fallback_chat_id
    )
    if not destinations:
        logger.warning(
            "LLM billing reminder has no destinations "
            "(owners with dm_ok, LLM_BILLING_REMINDER_CHAT_ID, or ADMIN_ALERT_CHAT_ID)"
        )
        return {
            "status": "skipped",
            "reason": "no_destinations",
        }

    period_key = period_key_for_datetime(datetime.now(UTC), settings)
    reminder_id = await reserve_reminder(session, period_key)
    if reminder_id is None:
        return {
            "status": "already_sent",
            "period_key": period_key,
        }

    # Commit the reservation before sending so a crash cannot double-send.
    await session.commit()

    sent = 0
    failed = 0
    for chat_id in destinations:
        try:
            await telegram.send_message(chat_id, reminder_text)
            sent += 1
        except TelegramSendError:
            failed += 1

    if sent == 0:
        # Release the reservation so a manual retry can recover.
        await release_reminder(session, reminder_id)
        await session.commit()
        return {
            "status": "degraded",
            "period_key": period_key,
            "sent": sent,
            "failed": failed,
        }

    await record_reminder_sent(session, reminder_id, destinations)
    await session.commit()

    if failed > 0:
        logger.warning(
            "LLM billing reminder partial failure sent={} failed={}",
            sent,
            failed,
        )

    return {
        "status": "ok",
        "period_key": period_key,
        "sent": sent,
        "failed": failed,
    }
