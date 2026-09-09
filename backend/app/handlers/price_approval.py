"""Price draft approval via slash command or inline button."""

from __future__ import annotations

import re
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.price_service import approve_price_draft, reject_price_draft
from app.telegram.client import TelegramClientProtocol, get_telegram_client
from app.telegram.keyboards import CallbackData
from app.templates.messages_ru import render_template
from app.utils.telegram import extract_message_text
from app.utils.whitelist import PriceApprover, resolve_price_approver

_APPROVE_PRICE_RE = re.compile(
    r"^/approve_price(?:@\w+)?\s+(\d+)\s*$",
    re.IGNORECASE,
)
_REJECT_PRICE_RE = re.compile(
    r"^/reject_price(?:@\w+)?\s+(\d+)\s*$",
    re.IGNORECASE,
)

Decision = Literal["approve", "reject"]


def _message_text(message: dict[str, Any]) -> str:
    return extract_message_text(message)


async def _apply_decision(
    session: AsyncSession,
    *,
    draft_id: int,
    decision: Decision,
    approver: PriceApprover,
    telegram: TelegramClientProtocol,
) -> tuple[str, str]:
    if decision == "approve":
        outcome = await approve_price_draft(
            session,
            draft_id=draft_id,
            employee_id=approver.employee_id,
            approver_telegram_id=approver.telegram_id,
            telegram=telegram,
        )
        if outcome == "not_found":
            return outcome, render_template("price_draft_not_found", draft_id=draft_id)
        if outcome == "already_processed":
            return outcome, render_template(
                "price_draft_already_processed", draft_id=draft_id
            )
        if outcome == "approved_partial":
            return (
                outcome,
                "Прайс утверждён, но публикация прошла частично. Проверьте логи отправки.",
            )
        return outcome, render_template("price_approved", draft_id=draft_id)

    outcome = await reject_price_draft(
        session,
        draft_id=draft_id,
        employee_id=approver.employee_id,
        approver_telegram_id=approver.telegram_id,
    )
    if outcome == "not_found":
        return outcome, render_template("price_draft_not_found", draft_id=draft_id)
    if outcome == "already_processed":
        return outcome, render_template(
            "price_draft_already_processed", draft_id=draft_id
        )
    return outcome, render_template("price_rejected", draft_id=draft_id)


async def handle_price_command(
    session: AsyncSession,
    message: dict[str, Any],
    *,
    approver: PriceApprover,
    telegram: TelegramClientProtocol,
) -> str:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return "ignored"

    text = _message_text(message)
    decision: Decision | None = None
    draft_id: int | None = None

    if text.startswith("/approve_price"):
        match = _APPROVE_PRICE_RE.match(text)
        if match:
            decision = "approve"
            draft_id = int(match.group(1))
    elif text.startswith("/reject_price"):
        match = _REJECT_PRICE_RE.match(text)
        if match:
            decision = "reject"
            draft_id = int(match.group(1))

    if decision is None or draft_id is None:
        await telegram.send_message(int(chat_id), render_template("invalid_command"))
        return "ok"

    _, response_text = await _apply_decision(
        session,
        draft_id=draft_id,
        decision=decision,
        approver=approver,
        telegram=telegram,
    )
    await telegram.send_message(int(chat_id), response_text)
    return "ok"


async def handle_price_callback(
    session: AsyncSession,
    callback_query: dict[str, Any],
) -> str:
    telegram = get_telegram_client()
    from_user = callback_query.get("from") or {}
    telegram_id = from_user.get("id")
    callback_id = callback_query.get("id", "")
    if telegram_id is None:
        return "ignored"

    approver = await resolve_price_approver(session, int(telegram_id))
    if approver is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"

    cd = CallbackData.decode(callback_query.get("data"))
    if cd is None or cd.namespace != "price" or cd.action not in ("approve", "reject"):
        return "ignored"

    message = callback_query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id", 0))
    message_id = message.get("message_id")
    draft_id = cd.arg
    decision: Decision = "approve" if cd.action == "approve" else "reject"

    outcome, response_text = await _apply_decision(
        session,
        draft_id=draft_id,
        decision=decision,
        approver=approver,
        telegram=telegram,
    )

    if outcome == "already_processed":
        await telegram.answer_callback_query(
            callback_id, text=response_text, show_alert=True
        )
    else:
        await telegram.answer_callback_query(callback_id)

    if message_id is not None:
        try:
            await telegram.edit_message_text(
                chat_id,
                int(message_id),
                response_text,
                reply_markup=None,
            )
        except Exception:
            pass

    return "ok"
