"""Employee command handlers — /ask, /help, Phase 2–5 commands (TECH DOC §9, §11)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, tzinfo
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Deal, Quote, QuoteSource, Request, RequestStatus, Supplier
from app.parsers.nl_commands import parse_nl_command
from app.services.bargain_service import (
    BargainStatusError,
    NoEligibleQuoteError,
    SupplierUnavailableError,
    start_bargain,
)
from app.services.deal_service import (
    DealAlreadyExistsError,
    RequestNotFoundError,
    RequestNotOpenError,
    SupplierNotFoundError,
    cancel_request,
    create_deal,
)
from app.services.price_service import approve_price_draft, reject_price_draft
from app.services.quote_service import upsert_quote
from app.services.recheck_service import (
    RECHECK_HOURS_DEFAULT,
    RECHECK_HOURS_MAX,
    RECHECK_HOURS_MIN,
    InvalidRecheckHoursError,
    schedule_recheck,
)
from app.services.request_service import create_request, load_request_for_employee
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import format_supplier_label, render_template
from app.utils.telegram import extract_message_text
from app.utils.whitelist import get_employee_by_telegram_id

_ASK_COMMAND_RE = re.compile(r"^/ask(?:@\w+)?(?:\s|$)", re.IGNORECASE)
_SETPRICE_COMMAND_RE = re.compile(
    r"^/setprice(?:@\w+)?\s+(\d+)\s+(\d+)\s+([\d.]+)(?:\s+(\d+))?\s*$",
    re.IGNORECASE,
)
_DEAL_COMMAND_RE = re.compile(
    r"^/deal(?:@\w+)?\s+(\d+)\s+(\d+)\s+([\d.]+)\s*$",
    re.IGNORECASE,
)
_STATUS_COMMAND_RE = re.compile(r"^/status(?:@\w+)?\s+(\d+)\s*$", re.IGNORECASE)
_CANCEL_COMMAND_RE = re.compile(r"^/cancel(?:@\w+)?\s+(\d+)\s*$", re.IGNORECASE)
_BARGAIN_COMMAND_RE = re.compile(
    r"^/bargain(?:@\w+)?\s+(\d+)\s+([\d.]+)(?:\s+(\d+))?\s*$",
    re.IGNORECASE,
)
_RECHECK_COMMAND_RE = re.compile(
    r"^/recheck(?:@\w+)?\s+(\d+)(?:\s+(\d+))?\s*$",
    re.IGNORECASE,
)
_APPROVE_PRICE_RE = re.compile(
    r"^/approve_price(?:@\w+)?\s+(\d+)\s*$",
    re.IGNORECASE,
)
_REJECT_PRICE_RE = re.compile(
    r"^/reject_price(?:@\w+)?\s+(\d+)\s*$",
    re.IGNORECASE,
)


def extract_ask_text(message: dict) -> str:
    """Build request text from reply + text after ``/ask``."""
    parts: list[str] = []
    reply = message.get("reply_to_message")
    if reply:
        reply_text = reply.get("text") or reply.get("caption") or ""
        if reply_text.strip():
            parts.append(reply_text.strip())

    text = (message.get("text") or message.get("caption") or "").strip()
    if _ASK_COMMAND_RE.match(text):
        remainder = _ASK_COMMAND_RE.sub("", text, count=1).strip()
        if remainder:
            parts.append(remainder)

    return "\n".join(parts).strip()


def is_ask_command(message: dict) -> bool:
    text = extract_message_text(message)
    return bool(_ASK_COMMAND_RE.match(text))


def is_help_command(message: dict) -> bool:
    text = extract_message_text(message)
    return text.startswith("/help")


def _message_text(message: dict) -> str:
    return extract_message_text(message)


def _parse_decimal_token(raw: str) -> Decimal | None:
    normalized = raw.replace(",", ".").strip()
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    if value < 0:
        return None
    return value


def _format_quotes_block(quotes: list[Quote]) -> str:
    if not quotes:
        return "—"
    lines: list[str] = []
    for quote in quotes:
        supplier_label = format_supplier_label(
            quote.supplier.name if quote.supplier else None, quote.supplier_id
        )
        price = quote.price_initial
        price_text = f"{price} ₽" if price is not None else "—"
        qty_text = str(quote.qty) if quote.qty is not None else "—"
        if quote.available is True:
            avail_text = "есть"
        elif quote.available is False:
            avail_text = "нет"
        else:
            avail_text = "—"
        lines.append(
            f"- {supplier_label}: {price_text}, кол-во {qty_text}, наличие: {avail_text}"
        )
    return "\n".join(lines)


def _format_deal_block(deal: Deal | None) -> str:
    if deal is None:
        return ""
    supplier_label = format_supplier_label(
        deal.chosen_supplier.name if deal.chosen_supplier else None,
        deal.chosen_supplier_id,
    )
    final_price = deal.final_price
    price_text = f"{final_price} ₽" if final_price is not None else "—"
    return f"\nСделка: {supplier_label}, итог {price_text}"


def _format_due_at(dt: datetime) -> str:
    settings = get_settings()
    try:
        tz: tzinfo = ZoneInfo(settings.tz)
    except Exception:
        tz = UTC
    local = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=UTC).astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M %Z")


async def _run_bargain(
    session: AsyncSession,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
    request_id: int,
    target_price: Decimal,
    supplier_id: int | None = None,
) -> str:
    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=is_private_chat,
    )
    if request is None:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"

    try:
        outbound = await start_bargain(
            session,
            request_id=request_id,
            target_price=target_price,
            telegram=telegram,
            supplier_id=supplier_id,
        )
    except RequestNotFoundError:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"
    except BargainStatusError:
        status = request.status.value
        await telegram.send_message(
            chat_id,
            render_template(
                "bargain_not_allowed",
                request_id=request_id,
                status=status,
            ),
        )
        return "ok"
    except NoEligibleQuoteError:
        await telegram.send_message(
            chat_id,
            render_template("bargain_no_quote", request_id=request_id),
        )
        return "ok"
    except SupplierNotFoundError:
        await telegram.send_message(
            chat_id,
            render_template(
                "supplier_not_found",
                supplier_id=supplier_id if supplier_id is not None else 0,
            ),
        )
        return "ok"
    except SupplierUnavailableError:
        await telegram.send_message(
            chat_id,
            render_template(
                "bargain_supplier_unavailable",
                request_id=request_id,
            ),
        )
        return "ok"

    supplier = await session.get(Supplier, outbound.supplier_id)
    await telegram.send_message(
        chat_id,
        render_template(
            "bargain_sent",
            request_id=request_id,
            target_price=target_price,
            supplier_name=supplier.name if supplier else None,
            supplier_id=outbound.supplier_id,
        ),
    )
    return "ok"


async def _run_recheck(
    session: AsyncSession,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
    request_id: int,
    hours: int = RECHECK_HOURS_DEFAULT,
) -> str:
    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=is_private_chat,
    )
    if request is None:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"

    try:
        request = await schedule_recheck(
            session,
            request_id=request_id,
            hours=hours,
        )
    except RequestNotFoundError:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"
    except RequestNotOpenError:
        await telegram.send_message(
            chat_id,
            render_template("request_not_open", request_id=request_id),
        )
        return "ok"
    except InvalidRecheckHoursError:
        await telegram.send_message(
            chat_id,
            render_template(
                "recheck_invalid_hours",
                min_hours=RECHECK_HOURS_MIN,
                max_hours=RECHECK_HOURS_MAX,
            ),
        )
        return "ok"

    due_text = _format_due_at(request.recheck_at) if request.recheck_at else "—"
    await telegram.send_message(
        chat_id,
        render_template(
            "recheck_scheduled",
            request_id=request_id,
            hours=hours,
            due_at=due_text,
        ),
    )
    return "ok"


async def _handle_bargain(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _BARGAIN_COMMAND_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    request_id = int(match.group(1))
    target_price = _parse_decimal_token(match.group(2))
    if target_price is None:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"
    supplier_id = int(match.group(3)) if match.group(3) else None
    return await _run_bargain(
        session,
        chat_id=chat_id,
        employee_id=employee_id,
        is_private_chat=is_private_chat,
        telegram=telegram,
        request_id=request_id,
        target_price=target_price,
        supplier_id=supplier_id,
    )


async def _handle_recheck(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _RECHECK_COMMAND_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    request_id = int(match.group(1))
    hours = int(match.group(2)) if match.group(2) else RECHECK_HOURS_DEFAULT
    return await _run_recheck(
        session,
        chat_id=chat_id,
        employee_id=employee_id,
        is_private_chat=is_private_chat,
        telegram=telegram,
        request_id=request_id,
        hours=hours,
    )


async def _handle_nl_command(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    reply = message.get("reply_to_message") or {}
    reply_text = reply.get("text") or reply.get("caption") or None
    parsed = parse_nl_command(text, reply_text=reply_text)
    if parsed is None:
        return "ignored"

    if parsed.intent == "deal":
        if (
            parsed.request_id is None
            or parsed.supplier_id is None
            or parsed.price is None
        ):
            await telegram.send_message(
                chat_id,
                render_template("deal_need_args"),
            )
            return "ok"
        return await _run_deal(
            session,
            chat_id=chat_id,
            employee_id=employee_id,
            is_private_chat=is_private_chat,
            telegram=telegram,
            request_id=parsed.request_id,
            supplier_id=parsed.supplier_id,
            final_price=parsed.price,
        )

    if parsed.request_id is None:
        return "ignored"

    if parsed.intent == "recheck":
        hours = (
            parsed.hours
            if parsed.hours is not None
            else RECHECK_HOURS_DEFAULT
        )
        return await _run_recheck(
            session,
            chat_id=chat_id,
            employee_id=employee_id,
            is_private_chat=is_private_chat,
            telegram=telegram,
            request_id=parsed.request_id,
            hours=hours,
        )

    if parsed.target_price is None:
        await telegram.send_message(
            chat_id,
            render_template("bargain_need_price"),
        )
        return "ok"

    return await _run_bargain(
        session,
        chat_id=chat_id,
        employee_id=employee_id,
        is_private_chat=is_private_chat,
        telegram=telegram,
        request_id=parsed.request_id,
        target_price=parsed.target_price,
    )


async def _handle_setprice(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _SETPRICE_COMMAND_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    request_id = int(match.group(1))
    supplier_id = int(match.group(2))
    price = _parse_decimal_token(match.group(3))
    if price is None:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"
    qty = int(match.group(4)) if match.group(4) else None

    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=is_private_chat,
    )
    if request is None:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"

    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        await telegram.send_message(
            chat_id,
            render_template("request_not_open", request_id=request_id),
        )
        return "ok"

    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        await telegram.send_message(
            chat_id,
            render_template("supplier_not_found", supplier_id=supplier_id),
        )
        return "ok"

    await upsert_quote(
        session,
        request_id,
        supplier_id,
        price=price,
        qty=qty,
        source=QuoteSource.manual,
        confidence=1.0,
    )
    await telegram.send_message(
        chat_id,
        render_template(
            "setprice_ok",
            request_id=request_id,
            supplier_name=supplier.name,
            supplier_id=supplier_id,
            price=price,
        ),
    )
    return "ok"


async def _run_deal(
    session: AsyncSession,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
    request_id: int,
    supplier_id: int,
    final_price: Decimal,
) -> str:
    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=is_private_chat,
    )
    if request is None:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"

    try:
        deal = await create_deal(
            session,
            request_id=request_id,
            supplier_id=supplier_id,
            final_price=final_price,
        )
    except RequestNotFoundError:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"
    except SupplierNotFoundError:
        await telegram.send_message(
            chat_id,
            render_template("supplier_not_found", supplier_id=supplier_id),
        )
        return "ok"
    except RequestNotOpenError:
        await telegram.send_message(
            chat_id,
            render_template("request_not_open", request_id=request_id),
        )
        return "ok"
    except DealAlreadyExistsError:
        await telegram.send_message(
            chat_id,
            render_template("deal_already_exists", request_id=request_id),
        )
        return "ok"

    supplier = await session.get(Supplier, deal.chosen_supplier_id)
    await telegram.send_message(
        chat_id,
        render_template(
            "deal_closed",
            request_id=request_id,
            supplier_name=supplier.name if supplier else None,
            supplier_id=deal.chosen_supplier_id,
            final_price=final_price,
        ),
    )
    return "ok"


async def _handle_deal(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _DEAL_COMMAND_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    request_id = int(match.group(1))
    supplier_id = int(match.group(2))
    final_price = _parse_decimal_token(match.group(3))
    if final_price is None:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    return await _run_deal(
        session,
        chat_id=chat_id,
        employee_id=employee_id,
        is_private_chat=is_private_chat,
        telegram=telegram,
        request_id=request_id,
        supplier_id=supplier_id,
        final_price=final_price,
    )


async def _handle_status(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _STATUS_COMMAND_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    request_id = int(match.group(1))
    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=is_private_chat,
    )
    if request is None:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"

    result = await session.execute(
        select(Request)
        .where(Request.id == request_id)
        .options(
            selectinload(Request.quotes).selectinload(Quote.supplier),
            selectinload(Request.deals).selectinload(Deal.chosen_supplier),
        )
    )
    request = result.scalar_one_or_none()
    if request is None:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"

    deal = request.deals[0] if request.deals else None
    await telegram.send_message(
        chat_id,
        render_template(
            "status_summary",
            request_id=request.id,
            status=request.status.value,
            source_text=request.source_text,
            quotes_block=_format_quotes_block(request.quotes),
            deal_block=_format_deal_block(deal),
        ),
    )
    return "ok"


async def _handle_cancel(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    is_private_chat: bool,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _CANCEL_COMMAND_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    request_id = int(match.group(1))
    request = await load_request_for_employee(
        session,
        request_id=request_id,
        employee_id=employee_id,
        chat_id=chat_id,
        is_private_chat=is_private_chat,
    )
    if request is None:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"

    try:
        await cancel_request(session, request_id=request_id)
    except RequestNotFoundError:
        await telegram.send_message(
            chat_id,
            render_template("request_not_found", request_id=request_id),
        )
        return "ok"
    except RequestNotOpenError:
        await telegram.send_message(
            chat_id,
            render_template("request_not_open", request_id=request_id),
        )
        return "ok"

    await telegram.send_message(
        chat_id,
        render_template("cancel_ok", request_id=request_id),
    )
    return "ok"


async def _handle_approve_price(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _APPROVE_PRICE_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    draft_id = int(match.group(1))
    outcome = await approve_price_draft(
        session,
        draft_id=draft_id,
        employee_id=employee_id,
        telegram=telegram,
    )
    if outcome == "not_found":
        await telegram.send_message(
            chat_id,
            render_template("price_draft_not_found", draft_id=draft_id),
        )
    elif outcome == "already_processed":
        await telegram.send_message(
            chat_id,
            render_template("price_draft_already_processed", draft_id=draft_id),
        )
    elif outcome == "approved_partial":
        await telegram.send_message(
            chat_id,
            "Прайс утверждён, но публикация прошла частично. Проверьте логи отправки.",
        )
    else:
        await telegram.send_message(
            chat_id,
            render_template("price_approved", draft_id=draft_id),
        )
    return "ok"


async def _handle_reject_price(
    session: AsyncSession,
    message: dict,
    *,
    chat_id: int,
    employee_id: int,
    telegram: TelegramClientProtocol,
) -> str:
    text = _message_text(message)
    match = _REJECT_PRICE_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, render_template("invalid_command"))
        return "ok"

    draft_id = int(match.group(1))
    outcome = await reject_price_draft(
        session,
        draft_id=draft_id,
        employee_id=employee_id,
    )
    if outcome == "not_found":
        await telegram.send_message(
            chat_id,
            render_template("price_draft_not_found", draft_id=draft_id),
        )
    elif outcome == "already_processed":
        await telegram.send_message(
            chat_id,
            render_template("price_draft_already_processed", draft_id=draft_id),
        )
    else:
        await telegram.send_message(
            chat_id,
            render_template("price_rejected", draft_id=draft_id),
        )
    return "ok"


async def handle_employee_message(
    session: AsyncSession,
    message: dict,
    *,
    telegram: TelegramClientProtocol,
) -> str:
    """
    Handle employee group command.

    Returns webhook status: ``ok`` or ``ignored``.
    """
    from_user = message.get("from") or {}
    telegram_id = from_user.get("id")
    if telegram_id is None:
        return "ignored"

    employee = await get_employee_by_telegram_id(
        session, int(telegram_id), require_active=True
    )
    if employee is None:
        return "ignored"

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = (chat.get("type") or "").lower()
    if chat_id is None:
        return "ignored"
    is_private_chat = chat_type == "private"

    if is_help_command(message):
        await telegram.send_message(int(chat_id), render_template("help"))
        return "ok"

    text = _message_text(message)

    if text.startswith("/setprice"):
        return await _handle_setprice(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )

    if text.startswith("/deal"):
        return await _handle_deal(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )

    if text.startswith("/bargain"):
        return await _handle_bargain(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )

    if text.startswith("/recheck"):
        return await _handle_recheck(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )

    if text.startswith("/status"):
        return await _handle_status(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )

    if text.startswith("/cancel"):
        return await _handle_cancel(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )

    if text.startswith("/approve_price"):
        return await _handle_approve_price(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            telegram=telegram,
        )

    if text.startswith("/reject_price"):
        return await _handle_reject_price(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            telegram=telegram,
        )

    if not is_ask_command(message):
        return await _handle_nl_command(
            session,
            message,
            chat_id=int(chat_id),
            employee_id=employee.id,
            is_private_chat=is_private_chat,
            telegram=telegram,
        )

    text_req = extract_ask_text(message)
    max_len = get_settings().max_message_text_len
    if len(text_req) > max_len:
        text_req = text_req[:max_len]
    if not text_req:
        await telegram.send_message(int(chat_id), render_template("ask_empty"))
        return "ok"

    request, sent_count = await create_request(
        session,
        group_chat_id=int(chat_id),
        employee_id=employee.id,
        source_text=text_req,
        telegram=telegram,
    )
    ack = render_template(
        "ask_sent",
        request_id=request.id,
        N=sent_count,
    )
    await telegram.send_message(int(chat_id), ack)
    return "ok"
