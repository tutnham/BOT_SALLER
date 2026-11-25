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
    MARKUP_CATEGORIES,
    get_markup,
    list_active_rules,
    markup_category_arg,
    markup_category_code,
    markup_category_label,
    markup_rule_label,
    model_sort_key,
    representative_rules,
    retire_shadowed_custom_rules,
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


def _amount_text(rule: MarkupRule) -> str:
    if rule.markup_fixed is None:
        return "не задана"
    return f"{int(rule.markup_fixed)} ₽"


async def _show_markup(
    telegram: TelegramClientProtocol,
    *,
    chat_id: int,
    text: str,
    rows: list[list[dict]],
    message_id: int | None,
) -> None:
    from app.handlers.admin_menu import _send_or_edit
    from app.telegram.keyboards import inline_keyboard as keyboard

    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=keyboard(rows),
    )


async def send_markup_list(
    session: AsyncSession,
    *,
    chat_id: int,
    telegram: TelegramClientProtocol,
    message_id: int | None = None,
) -> None:
    await retire_shadowed_custom_rules(session)
    rules = await list_active_rules(session)
    reps = representative_rules(rules)
    present = {markup_category_code(rule) for rule in reps}
    rows: list[list[dict]] = []
    for arg, label, code in MARKUP_CATEGORIES:
        if code not in present:
            continue
        rows.append([menu_button(label, "mk_cat", arg)])
    rows.append([menu_button("Добавить правило", "mk_add")])
    rows.append([menu_button("В меню", "main_menu")])
    text = (
        render_template("markup_rules_list")
        if reps
        else "Наценок пока нет."
    )
    await _show_markup(
        telegram, chat_id=chat_id, text=text, rows=rows, message_id=message_id
    )


async def send_markup_category(
    session: AsyncSession,
    *,
    chat_id: int,
    telegram: TelegramClientProtocol,
    category_arg: int,
    page: int = 0,
    message_id: int | None = None,
) -> None:
    rules = await list_active_rules(session)
    code = "other"
    for arg, _label, stored in MARKUP_CATEGORIES:
        if arg == category_arg:
            code = stored
            break
    models = sorted(
        (rule for rule in representative_rules(rules) if markup_category_code(rule) == code),
        key=model_sort_key,
    )
    page_size = 8
    page = max(page, 0)
    visible = models[page * page_size : (page + 1) * page_size]
    rows: list[list[dict]] = []
    for rule in visible:
        amount = "—" if rule.markup_fixed is None else f"{int(rule.markup_fixed)} ₽"
        label = f"{markup_rule_label(rule)} · {amount}"
        rows.append([menu_button(label[:60], "mk_open", int(rule.id))])
    nav: list[dict] = []
    if page > 0:
        nav.append(menu_button("◀ Назад", "mk_cat", category_arg, page=page - 1))
    if (page + 1) * page_size < len(models):
        nav.append(menu_button("Вперед ▶", "mk_cat", category_arg, page=page + 1))
    if nav:
        rows.append(nav)
    rows.append([menu_button("Добавить правило", "mk_add")])
    rows.append(
        [
            menu_button("К наценкам", "markup_list"),
            menu_button("В меню", "main_menu"),
        ]
    )
    await _show_markup(
        telegram,
        chat_id=chat_id,
        text=render_template("markup_category", category=markup_category_label(category_arg)),
        rows=rows,
        message_id=message_id,
    )


async def _today_variant_lines(
    session: AsyncSession,
    rule: MarkupRule,
    rules: list[MarkupRule],
) -> str:
    from app.parsers.product_normalizer import canonicalize_catalog_fields
    from app.services.price_service import aggregate_today, format_rub_amount

    lines: list[str] = []
    for row in await aggregate_today(session):
        product = {"model": row.get("model"), "storage": row.get("storage")}
        if get_markup(product, rules).rule_id != rule.id:
            continue
        _model, storage = canonicalize_catalog_fields(row.get("model"), row.get("storage"))
        parts = [
            part
            for part in (storage, row.get("color"), row.get("region"), row.get("sim"))
            if part
        ]
        title = " ".join(str(part) for part in parts) or "—"
        lines.append(f"{title} — {format_rub_amount(row['min_price'])}")
    if not lines:
        return "Сегодня цен нет."
    if len(lines) > 20:
        hidden = len(lines) - 20
        lines = [*lines[:20], f"… и ещё {hidden}"]
    return "\n".join(lines)


async def send_markup_edit(
    session: AsyncSession,
    *,
    chat_id: int,
    rule_id: int,
    telegram: TelegramClientProtocol,
    message_id: int | None = None,
) -> None:
    rule = await session.get(MarkupRule, rule_id)
    if rule is None:
        await _show_markup(
            telegram,
            chat_id=chat_id,
            text=render_template("markup_rule_not_found", rule_key=rule_id),
            rows=[[menu_button("К наценкам", "markup_list")]],
            message_id=message_id,
        )
        return
    rules = await list_active_rules(session)
    prices = await _today_variant_lines(session, rule, rules)
    amount_row = [
        menu_button(f"{preset}", "mk_set", rule_id, page=preset)
        for preset in (500, 800, 1000)
    ]
    rows = [
        amount_row,
        [menu_button("Своя сумма", "mk_custom", rule_id)],
        [
            menu_button("К моделям", "mk_cat", markup_category_arg(markup_category_code(rule))),
            menu_button("В меню", "main_menu"),
        ],
    ]
    await _show_markup(
        telegram,
        chat_id=chat_id,
        text=render_template(
            "markup_edit_prompt",
            rule_label=markup_rule_label(rule),
            amount=_amount_text(rule),
            prices_block=prices,
        ),
        rows=rows,
        message_id=message_id,
    )


async def decline_markup_prompt(
    session: AsyncSession,
    *,
    chat_id: int,
    rule_id: int,
    telegram: TelegramClientProtocol,
) -> None:
    from app.services.markup_service import decline_unpriced_markup_rule

    outcome = await decline_unpriced_markup_rule(session, rule_id)
    rule = await session.get(MarkupRule, rule_id)
    label = markup_rule_label(rule) if rule is not None else str(rule_id)
    if outcome == "missing":
        text = render_template("markup_rule_not_found", rule_key=rule_id)
    elif outcome == "already_priced":
        text = render_template("markup_product_already_priced", rule_label=label)
    else:
        text = render_template("markup_product_declined", rule_label=label)
    await telegram.send_message(chat_id, text)


async def apply_markup_choice(
    session: AsyncSession,
    *,
    chat_id: int,
    rule_id: int,
    amount: Decimal,
    telegram: TelegramClientProtocol,
    message_id: int | None = None,
) -> None:
    updated = await update_rule_markup_by_id(session, rule_id, amount)
    if updated is None:
        await telegram.send_message(
            chat_id,
            render_template("markup_rule_not_found", rule_key=rule_id),
        )
        return
    if message_id is not None:
        await send_markup_edit(
            session,
            chat_id=chat_id,
            rule_id=rule_id,
            telegram=telegram,
            message_id=message_id,
        )
        return
    await telegram.send_message(
        chat_id,
        render_template(
            "markup_rule_updated",
            rule_key=markup_rule_label(updated),
            amount=amount,
        ),
        reply_markup=inline_keyboard(
            [
                [
                    menu_button("К наценкам", "markup_list"),
                    menu_button("В меню", "main_menu"),
                ]
            ]
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

    from app.handlers.purchase_report_commands import handle_purchase_report_command

    if await handle_purchase_report_command(
        session,
        text=text,
        chat_id=int(chat_id),
        telegram_id=int(telegram_id),
        telegram=telegram,
        attach_csv=True,
    ):
        return "ok"

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
