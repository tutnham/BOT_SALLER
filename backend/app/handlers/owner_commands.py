"""Owner command handlers for analytics reports and purge (TECH DOC §9.6, §11)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import MarkupRule, ProductCategory, Supplier, SupplierCategory
from app.handlers.supplier_messages import manual_bind_message
from app.services.markup_service import (
    list_active_rules,
    update_rule_markup,
    update_rule_markup_by_id,
)
from app.services.purge_service import DEFAULT_PURGE_OLD_LIMIT, purge_old_requests
from app.services.report_service import build_report
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import inline_keyboard, menu_button
from app.templates.messages_ru import render_template
from app.utils.html import TELEGRAM_HTML_PARSE_MODE
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
_MARKUP_SET_RE = re.compile(
    r"^/markup_set(?:@\w+)?\s+(\S+)\s+(\d+)\s*$",
    re.IGNORECASE,
)
_SUPPLIER_CAT_ADD_RE = re.compile(
    r"^/supplier_cat_add(?:@\w+)?\s+(\d+)\s+(\w+)\s*$",
    re.IGNORECASE,
)
_SUPPLIER_CAT_DEL_RE = re.compile(
    r"^/supplier_cat_del(?:@\w+)?\s+(\d+)\s+(\w+)\s*$",
    re.IGNORECASE,
)
_BIND_RE = re.compile(
    r"^/bind(?:@\w+)?\s+(\d+)\s+(\d+)\s*$",
    re.IGNORECASE,
)

_VALID_CATEGORIES = frozenset(item.value for item in ProductCategory)


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


async def _handle_markup_list(
    session: AsyncSession,
    *,
    chat_id: int,
    telegram: TelegramClientProtocol,
) -> str:
    await send_markup_list(session, chat_id=chat_id, telegram=telegram)
    return "ok"


def _markup_label(rule: MarkupRule) -> str:
    return (rule.rule_key or rule.category or "правило").replace("_", " ")


async def send_markup_list(
    session: AsyncSession,
    *,
    chat_id: int,
    telegram: TelegramClientProtocol,
) -> None:
    rules = await list_active_rules(session)
    if not rules:
        await telegram.send_message(
            chat_id,
            render_template("markup_rules_list", rules_block="—"),
        )
        return
    lines: list[str] = []
    rows: list[list[dict]] = []
    for rule in rules:
        label = _markup_label(rule)
        amount = int(rule.markup_fixed or 0)
        lines.append(f"{label}: {amount} ₽")
        rows.append(
            [menu_button(f"{label} · {amount} ₽"[:40], "mk_open", int(rule.id))]
        )
    rows.append([menu_button("Добавить правило", "mk_add")])
    await telegram.send_message(
        chat_id,
        render_template("markup_rules_list", rules_block="\n".join(lines)),
        reply_markup=inline_keyboard(rows),
    )


async def send_markup_edit(
    session: AsyncSession,
    *,
    chat_id: int,
    rule_id: int,
    telegram: TelegramClientProtocol,
) -> None:
    rule = await session.get(MarkupRule, rule_id)
    if rule is None:
        await telegram.send_message(chat_id, render_template("markup_rule_not_found", rule_key=rule_id))
        return
    label = _markup_label(rule)
    amount = int(rule.markup_fixed or 0)
    amount_row = [
        menu_button(f"{preset}", "mk_set", rule_id, page=preset)
        for preset in (500, 800, 1000)
    ]
    rows = [
        amount_row,
        [menu_button("Своя сумма", "mk_custom", rule_id)],
        [menu_button("К списку", "markup_list")],
    ]
    await telegram.send_message(
        chat_id,
        render_template("markup_edit_prompt", rule_label=label, amount=amount),
        reply_markup=inline_keyboard(rows),
    )


async def apply_markup_choice(
    session: AsyncSession,
    *,
    chat_id: int,
    rule_id: int,
    amount: Decimal,
    telegram: TelegramClientProtocol,
) -> None:
    updated = await update_rule_markup_by_id(session, rule_id, amount)
    if updated is None:
        await telegram.send_message(
            chat_id,
            render_template("markup_rule_not_found", rule_key=rule_id),
        )
        return
    await telegram.send_message(
        chat_id,
        render_template(
            "markup_rule_updated",
            rule_key=_markup_label(updated),
            amount=amount,
        ),
    )


async def _handle_markup_set(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
    telegram: TelegramClientProtocol,
) -> str:
    match = _MARKUP_SET_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, "Формат: /markup_set <rule_key> <сумма>")
        return "ok"
    rule_key = match.group(1)
    try:
        amount = Decimal(match.group(2))
    except InvalidOperation:
        await telegram.send_message(chat_id, "Сумма должна быть числом")
        return "ok"
    if amount < 0:
        await telegram.send_message(chat_id, "Сумма не может быть отрицательной")
        return "ok"
    updated = await update_rule_markup(session, rule_key, amount)
    if updated is None:
        await telegram.send_message(
            chat_id,
            render_template("markup_rule_not_found", rule_key=rule_key),
        )
        return "ok"
    await telegram.send_message(
        chat_id,
        render_template("markup_rule_updated", rule_key=rule_key, amount=amount),
    )
    return "ok"


async def _handle_supplier_categories_list(
    session: AsyncSession,
    *,
    chat_id: int,
    telegram: TelegramClientProtocol,
) -> str:
    result = await session.execute(
        select(Supplier)
        .options(selectinload(Supplier.categories))
        .order_by(Supplier.id.asc())
    )
    lines: list[str] = []
    for supplier in result.scalars().all():
        cats = sorted(row.category for row in supplier.categories)
        lines.append(f"#{supplier.id} {supplier.name}: {', '.join(cats) or '—'}")
    await telegram.send_message(
        chat_id,
        render_template(
            "supplier_categories_list",
            lines="\n".join(lines) if lines else "—",
        ),
    )
    return "ok"


async def _handle_supplier_cat_change(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
    telegram: TelegramClientProtocol,
    add: bool,
) -> str:
    pattern = _SUPPLIER_CAT_ADD_RE if add else _SUPPLIER_CAT_DEL_RE
    match = pattern.match(text)
    if not match:
        verb = "add" if add else "del"
        await telegram.send_message(
            chat_id,
            f"Формат: /supplier_cat_{verb} <supplier_id> <category>",
        )
        return "ok"
    supplier_id = int(match.group(1))
    category = match.group(2).lower()
    if category not in _VALID_CATEGORIES:
        await telegram.send_message(chat_id, f"Категория: {', '.join(sorted(_VALID_CATEGORIES))}")
        return "ok"
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        await telegram.send_message(chat_id, "Поставщик не найден")
        return "ok"
    if add:
        session.add(SupplierCategory(supplier_id=supplier_id, category=category))
    else:
        row = await session.scalar(
            select(SupplierCategory).where(
                SupplierCategory.supplier_id == supplier_id,
                SupplierCategory.category == category,
            )
        )
        if row is not None:
            await session.delete(row)
    await session.flush()
    await session.refresh(supplier, attribute_names=["categories"])
    cats = ", ".join(sorted(c.category for c in supplier.categories))
    await telegram.send_message(
        chat_id,
        render_template(
            "supplier_category_updated",
            supplier_id=supplier_id,
            categories=cats or "—",
        ),
    )
    return "ok"


async def _handle_bind(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
    telegram: TelegramClientProtocol,
) -> str:
    match = _BIND_RE.match(text)
    if not match:
        await telegram.send_message(chat_id, "Формат: /bind <message_in_id> <request_id>")
        return "ok"
    message_in_id = int(match.group(1))
    request_id = int(match.group(2))
    result = await manual_bind_message(
        session,
        message_in_id=message_in_id,
        request_id=request_id,
        telegram=telegram,
    )
    if result == "already_bound":
        await telegram.send_message(
            chat_id,
            render_template("bind_already", request_id=request_id),
        )
        return "ok"
    if result != "ok":
        await telegram.send_message(
            chat_id,
            render_template("bind_failed", reason=result),
        )
        return "ok"
    await telegram.send_message(
        chat_id,
        render_template("bind_ok", request_id=request_id),
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

    if text.startswith("/markup_set"):
        return await _handle_markup_set(
            session,
            chat_id=int(chat_id),
            text=text,
            telegram=telegram,
        )

    if text.startswith("/markup"):
        return await _handle_markup_list(
            session,
            chat_id=int(chat_id),
            telegram=telegram,
        )

    if text.startswith("/supplier_cat_add"):
        return await _handle_supplier_cat_change(
            session,
            chat_id=int(chat_id),
            text=text,
            telegram=telegram,
            add=True,
        )

    if text.startswith("/supplier_cat_del"):
        return await _handle_supplier_cat_change(
            session,
            chat_id=int(chat_id),
            text=text,
            telegram=telegram,
            add=False,
        )

    if text.startswith("/supplier_categories"):
        return await _handle_supplier_categories_list(
            session,
            chat_id=int(chat_id),
            telegram=telegram,
        )

    if text.startswith("/bind"):
        return await _handle_bind(
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
    await telegram.send_message(
        int(chat_id),
        report_text,
        parse_mode=TELEGRAM_HTML_PARSE_MODE,
    )
    return "ok"


async def send_owner_report(
    session: AsyncSession,
    *,
    chat_id: int,
    period: str,
    telegram: TelegramClientProtocol,
) -> None:
    report_text = await build_report(session, period=period, request_id=None)
    await telegram.send_message(
        chat_id,
        report_text,
        parse_mode=TELEGRAM_HTML_PARSE_MODE,
    )
