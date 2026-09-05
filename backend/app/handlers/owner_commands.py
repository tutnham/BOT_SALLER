"""Owner command handlers for analytics reports and purge (TECH DOC §9.6, §11)."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.purge_service import DEFAULT_PURGE_OLD_LIMIT, purge_old_requests
from app.services.report_service import build_report
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import inline_keyboard, menu_button
from app.templates.messages_ru import render_template
from app.utils.telegram import extract_message_text
from app.utils.whitelist import get_owner_by_telegram_id

_REPORT_RE = re.compile(r"^/report(?:@\w+)?(?:\s+(.+))?\s*$", re.IGNORECASE)
_STATS_RE = re.compile(r"^/stats(?:@\w+)?(?:\s+(.+))?\s*$", re.IGNORECASE)
_PURGE_REQUEST_RE = re.compile(
    r"^/purge_request(?:@\w+)?\s+(\d+)\s*$",
    re.IGNORECASE,
)
_PURGE_OLD_RE = re.compile(
    r"^/purge_old(?:@\w+)?\s+(\d+)(?:\s+(\d+))?(?:\s+(--confirm))?\s*$",
    re.IGNORECASE,
)


def _parse_report_args(text: str) -> tuple[str, int | None] | None:
    report_match = _REPORT_RE.match(text)
    if report_match:
        arg = (report_match.group(1) or "day").strip().lower()
        if arg in {"day", "week"}:
            return arg, None
        if arg.isdigit():
            return "day", int(arg)
        return None

    stats_match = _STATS_RE.match(text)
    if stats_match:
        arg = (stats_match.group(1) or "week").strip().lower()
        if arg == "week":
            return "week", None
        return None

    return None


def _format_id_list(ids: list[int]) -> str:
    if not ids:
        return "—"
    return ", ".join(f"#{request_id}" for request_id in ids)


async def _handle_purge_request(
    *,
    chat_id: int,
    text: str,
    telegram: TelegramClientProtocol,
) -> str:
    match = _PURGE_REQUEST_RE.match(text)
    if not match:
        await telegram.send_message(
            chat_id,
            render_template("purge_request_invalid"),
        )
        return "ok"

    request_id = int(match.group(1))
    markup = inline_keyboard(
        [
            [
                menu_button("Удалить", "purge_confirm", request_id),
                menu_button("Отмена", "purge_cancel", request_id),
            ]
        ]
    )
    await telegram.send_message(
        chat_id,
        render_template("purge_request_confirm", request_id=request_id),
        reply_markup=markup,
    )
    return "ok"


async def _handle_purge_old(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
    telegram: TelegramClientProtocol,
) -> str:
    match = _PURGE_OLD_RE.match(text)
    if not match:
        await telegram.send_message(
            chat_id,
            render_template("purge_old_invalid"),
        )
        return "ok"

    days = int(match.group(1))
    limit = int(match.group(2)) if match.group(2) else DEFAULT_PURGE_OLD_LIMIT
    dry_run = match.group(3) is None

    count, request_ids = await purge_old_requests(
        session,
        days=days,
        limit=limit,
        dry_run=dry_run,
    )
    ids_text = _format_id_list(request_ids)
    if dry_run:
        await telegram.send_message(
            chat_id,
            render_template(
                "purge_old_preview",
                count=count,
                days=days,
                ids=ids_text,
            ),
        )
    else:
        await telegram.send_message(
            chat_id,
            render_template(
                "purge_old_done",
                count=count,
                ids=ids_text,
            ),
        )
    return "ok"


async def handle_owner_message(
    session: AsyncSession,
    message: dict,
    *,
    telegram: TelegramClientProtocol,
) -> str:
    from_user = message.get("from") or {}
    telegram_id = from_user.get("id")
    if telegram_id is None:
        return "ignored"

    owner = await get_owner_by_telegram_id(session, int(telegram_id))
    if owner is None:
        return "ignored"
    if not owner.dm_ok:
        return "ignored"

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = extract_message_text(message)
    if chat_id is None or not text:
        return "ignored"

    if text.startswith("/purge_request"):
        return await _handle_purge_request(
            chat_id=int(chat_id),
            text=text,
            telegram=telegram,
        )

    if text.startswith("/purge_old"):
        return await _handle_purge_old(
            session,
            chat_id=int(chat_id),
            text=text,
            telegram=telegram,
        )

    parsed = _parse_report_args(text)
    if parsed is None:
        await telegram.send_message(
            int(chat_id),
            "Неверный формат команды. Используйте /report day, /report week, /report {id} или /stats week.",
        )
        return "ok"

    period, request_id = parsed
    report_text = await build_report(session, period=period, request_id=request_id)
    await telegram.send_message(int(chat_id), report_text)
    return "ok"
