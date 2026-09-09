"""Morning price pipeline (TECH DOC §9.5)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Owner, ParsedItem, PriceListDraft, RawPrice, Supplier
from app.llm.client import LLMProviderError, get_llm_client
from app.llm.schemas import (
    ParsedPriceItem,
    ParsedPriceList,
    validate_price_list_payload,
)
from app.parsers.cache import get_cached, normalize_input_text, set_cached
from app.services.alert_service import send_admin_alert
from app.services.markup_service import classify_category, load_rules, resolve_markup
from app.services.parser_client import ParserClientError, get_posts
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.telegram.keyboards import CallbackData, button, inline_keyboard
from app.templates.messages_ru import render_template


def build_raw_price_content_hash(supplier_id: int, raw_text: str) -> str:
    """D1: hash includes supplier_id so identical texts from two suppliers stay distinct."""
    normalized = normalize_input_text(raw_text)
    payload = f"{supplier_id}:{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()


def build_sku_key(item: ParsedPriceItem | dict[str, Any]) -> str:
    if isinstance(item, ParsedPriceItem):
        model = item.model or ""
        storage = item.storage or ""
        color = item.color or ""
        region = item.region or ""
        sim = item.sim or ""
    else:
        model = item.get("model") or ""
        storage = item.get("storage") or ""
        color = item.get("color") or ""
        region = item.get("region") or ""
        sim = item.get("sim") or ""
    return f"{model}|{storage}|{color}|{region}|{sim}"


def format_sku_title(
    *,
    model: str | None,
    storage: str | None,
    color: str | None,
    region: str | None,
    sim: str | None,
) -> str:
    parts = [p for p in (model, storage, color, region, sim) if p]
    return " ".join(parts).strip() or "—"


def _today_start_utc(tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    return start_local.astimezone(UTC)


def _item_passes_confidence(item: ParsedPriceItem, threshold: float) -> bool:
    return item.confidence >= threshold


async def insert_raw_price(
    session: AsyncSession,
    *,
    supplier_id: int,
    text: str,
    source: str,
    source_post_id: int | None = None,
    source_message_link: str | None = None,
) -> tuple[RawPrice | None, bool]:
    """
    Conflict-safe insert into ``raw_prices``.

    Returns ``(raw_price, is_new)``.
    On conflict: returns existing row (caller re-parses if ``parsed_items`` empty).
    """
    content_hash = build_raw_price_content_hash(supplier_id, text)
    stmt = (
        insert(RawPrice)
        .values(
            supplier_id=supplier_id,
            text=text,
            content_hash=content_hash,
            source=source,
            source_post_id=source_post_id,
            source_message_link=source_message_link,
        )
        .on_conflict_do_nothing(index_elements=["content_hash"])
        .returning(RawPrice.id)
    )
    result = await session.execute(stmt)
    new_id = result.scalar_one_or_none()
    if new_id is not None:
        raw = await session.get(RawPrice, new_id)
        return raw, True

    existing_result = await session.execute(
        select(RawPrice)
        .where(RawPrice.content_hash == content_hash)
        .options(selectinload(RawPrice.parsed_items))
    )
    existing = existing_result.scalar_one_or_none()
    return existing, False


async def ingest_channel_prices(
    session: AsyncSession,
) -> tuple[int, int]:
    """
    Fetch channel posts for suppliers with ``price_channel_id``.

    Returns ``(raw_prices_new, degraded_count)``.
    """
    result = await session.execute(
        select(Supplier).where(
            Supplier.active.is_(True),
            Supplier.price_channel_id.is_not(None),
        )
    )
    suppliers = list(result.scalars().all())
    raw_new = 0
    degraded = 0

    semaphore = asyncio.Semaphore(5)

    async def _fetch_posts_for_supplier(
        supplier: Supplier,
    ) -> tuple[Supplier, list[Any] | None, Exception | None]:
        channel_id = supplier.price_channel_id
        if channel_id is None:
            return supplier, None, None
        async with semaphore:
            try:
                posts = await get_posts(
                    channel_id=int(channel_id),
                    from_=supplier.last_price_sync_at,
                    content_type="text",
                )
                return supplier, posts, None
            except ParserClientError as exc:
                return supplier, None, exc

    fetch_results = await asyncio.gather(
        *[_fetch_posts_for_supplier(supplier) for supplier in suppliers]
    )

    for supplier, posts, fetch_error in fetch_results:
        channel_id = supplier.price_channel_id
        if channel_id is None:
            continue
        if fetch_error is not None:
            degraded += 1
            logger.warning(
                "Parser unavailable for supplier_id={} channel_id={}: {}",
                supplier.id,
                channel_id,
                fetch_error,
            )
            continue

        if posts is None:
            continue
        max_post_date: datetime | None = None
        for post in posts:
            text_body = (post.raw_text or "").strip()
            if not text_body:
                continue
            if max_post_date is None or post.post_date > max_post_date:
                max_post_date = post.post_date
            raw, is_new = await insert_raw_price(
                session,
                supplier_id=supplier.id,
                text=text_body,
                source="channel_post",
                source_post_id=post.post_id,
                source_message_link=post.message_link,
            )
            if is_new and raw is not None:
                raw_new += 1

        if max_post_date is not None:
            supplier.last_price_sync_at = max_post_date
            await session.flush()

    return raw_new, degraded


async def _resolve_price_list(
    session: AsyncSession,
    raw_text: str,
) -> ParsedPriceList | None:
    kind = "price_list"
    cached = await get_cached(session, kind=kind, raw_text=raw_text)
    if cached is not None:
        return validate_price_list_payload(cached)

    llm = get_llm_client()
    try:
        parsed = await llm.parse_price_list(raw_text)
    except LLMProviderError as exc:
        logger.warning("LLM parse_price_list failed: {}", exc)
        return None

    settings = get_settings()
    await set_cached(
        session,
        kind=kind,
        raw_text=raw_text,
        result_json=parsed.model_dump(mode="json"),
        model_used=settings.llm_model or settings.llm_provider,
    )
    return parsed


async def parse_pending_raw_prices(session: AsyncSession) -> int:
    """
    Parse today's raw_prices that still lack parsed_items.

    D2: low-confidence items are stored with a condition tag and skipped in aggregation.
    Returns number of newly materialised parsed_items rows.
    """
    settings = get_settings()
    day_start = _today_start_utc(settings.tz)
    threshold = settings.confidence_threshold

    result = await session.execute(
        select(RawPrice)
        .where(RawPrice.received_at >= day_start)
        .options(selectinload(RawPrice.parsed_items))
    )
    raw_prices = list(result.scalars().all())
    inserted = 0

    for raw in raw_prices:
        if raw.parsed_items:
            continue
        parsed = await _resolve_price_list(session, raw.text)
        if parsed is None:
            continue

        for item in parsed.items:
            condition = item.condition
            if not _item_passes_confidence(item, threshold):
                tag = f"low_confidence:{item.confidence}"
                condition = f"{tag}|{condition}" if condition else tag
            session.add(
                ParsedItem(
                    raw_price_id=raw.id,
                    sku_key=build_sku_key(item),
                    model=item.model,
                    storage=item.storage,
                    color=item.color,
                    region=item.region,
                    sim=item.sim,
                    condition=condition,
                    price=Decimal(str(item.price)),
                    currency=item.currency or "RUB",
                )
            )
            inserted += 1
        await session.flush()

    return inserted


async def aggregate_today(session: AsyncSession) -> list[dict[str, Any]]:
    """
    Min-price aggregation for today's parsed items (D2/D3).

    Groups by ``(sku_key, currency)``; draft path uses RUB only.
    """
    settings = get_settings()
    day_start = _today_start_utc(settings.tz)

    sql = text(
        """
        WITH today_items AS (
            SELECT
                pi.id,
                pi.sku_key,
                pi.model,
                pi.storage,
                pi.color,
                pi.region,
                pi.sim,
                pi.price,
                pi.currency,
                ROW_NUMBER() OVER (
                    PARTITION BY pi.sku_key, pi.currency
                    ORDER BY pi.price ASC, pi.id ASC
                ) AS rn
            FROM parsed_items pi
            JOIN raw_prices rp ON rp.id = pi.raw_price_id
            WHERE rp.received_at >= :day_start
              AND pi.currency = 'RUB'
              AND COALESCE(pi.condition, '') NOT LIKE 'low_confidence:%'
        )
        SELECT sku_key, model, storage, color, region, sim, price AS min_price, currency
        FROM today_items
        WHERE rn = 1
        ORDER BY sku_key
        """
    )
    result = await session.execute(sql, {"day_start": day_start})
    return [dict(row) for row in result.mappings().all()]


def build_draft_items(
    aggregated: list[dict[str, Any]],
    rules: dict,
    default_markup: Decimal,
) -> list[dict[str, Any]]:
    """Build public-safe draft items with markup applied."""
    draft_items: list[dict[str, Any]] = []
    for row in aggregated:
        currency = row.get("currency") or "RUB"
        if currency != "RUB":
            logger.warning(
                "Skipping non-RUB sku_key={} currency={}",
                row.get("sku_key"),
                currency,
            )
            continue

        category = classify_category(row.get("model"))
        markup = resolve_markup(rules, category, default_markup)
        if markup is None:
            logger.warning(
                "No markup rule for category={} sku_key={}",
                category,
                row.get("sku_key"),
            )
            continue

        min_price = Decimal(str(row["min_price"]))
        our_price = min_price + markup
        title = format_sku_title(
            model=row.get("model"),
            storage=row.get("storage"),
            color=row.get("color"),
            region=row.get("region"),
            sim=row.get("sim"),
        )
        draft_items.append(
            {
                "sku_key": row["sku_key"],
                "title": title,
                "our_price": str(our_price.quantize(Decimal("0.01"))),
                "currency": "RUB",
            }
        )
    return draft_items


def _draft_items_fingerprint(items: list[dict[str, Any]]) -> str:
    canonical = json.dumps(items, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _find_identical_pending_draft(
    session: AsyncSession,
    items: list[dict[str, Any]],
) -> PriceListDraft | None:
    """D4: reuse pending draft with identical items generated today."""
    settings = get_settings()
    day_start = _today_start_utc(settings.tz)
    fingerprint = _draft_items_fingerprint(items)

    result = await session.execute(
        select(PriceListDraft).where(
            PriceListDraft.status == "pending",
            PriceListDraft.generated_at >= day_start,
        )
    )
    for draft in result.scalars().all():
        existing = draft.items if isinstance(draft.items, list) else []
        if _draft_items_fingerprint(existing) == fingerprint:
            return draft
    return None


def _resolve_approval_chat_ids() -> list[int]:
    return get_settings().price_approval_chat_id_list


async def is_price_command_chat(session: AsyncSession, chat_id: int) -> bool:
    """True when chat receives morning price draft notifications."""
    destinations = await resolve_price_draft_destinations(session)
    return chat_id in destinations


def _price_draft_keyboard(draft_id: int) -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                button(
                    "✅ Утвердить",
                    cd=CallbackData(namespace="price", action="approve", arg=draft_id),
                ),
                button(
                    "❌ Отклонить",
                    cd=CallbackData(namespace="price", action="reject", arg=draft_id),
                ),
            ]
        ]
    )


async def resolve_price_draft_destinations(session: AsyncSession) -> list[int]:
    """Env approval chats plus every owner with ``dm_ok`` (deduped, env first)."""
    ids: list[int] = []
    seen: set[int] = set()
    for chat_id in _resolve_approval_chat_ids():
        if chat_id not in seen:
            seen.add(chat_id)
            ids.append(chat_id)
    result = await session.execute(
        select(Owner.telegram_id).where(Owner.dm_ok.is_(True))
    )
    for telegram_id in result.scalars().all():
        chat_id = int(telegram_id)
        if chat_id not in seen:
            seen.add(chat_id)
            ids.append(chat_id)
    return ids


def format_draft_lines(items: list[dict[str, Any]]) -> str:
    if not items:
        return "—"
    lines: list[str] = []
    for item in items:
        title = item.get("title") or item.get("sku_key") or "—"
        price = item.get("our_price")
        lines.append(f"{title} — {price} ₽")
    return "\n".join(lines)


async def build_morning_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    """
    Full §9.5 pipeline: ingest → parse → aggregate → markup → draft → notify.
    """
    settings = get_settings()
    channel_new, degraded = await ingest_channel_prices(session)
    await parse_pending_raw_prices(session)
    aggregated = await aggregate_today(session)
    rules = await load_rules(session)
    draft_items = build_draft_items(aggregated, rules, settings.default_markup)

    if degraded > 0:
        await send_admin_alert(
            render_template(
                "alert_morning_price_degraded",
                degraded_count=degraded,
            ),
            telegram=telegram,
        )

    if not draft_items:
        await send_admin_alert(
            render_template("alert_morning_price_empty"),
            telegram=telegram,
        )
        return {
            "status": "ok",
            "draft_id": None,
            "items": 0,
            "raw_prices_new": channel_new,
            "degraded": degraded,
        }

    existing = await _find_identical_pending_draft(session, draft_items)
    if existing is not None:
        return {
            "status": "ok",
            "draft_id": existing.id,
            "items": len(draft_items),
            "raw_prices_new": channel_new,
            "degraded": degraded,
            "reused": True,
        }

    draft = PriceListDraft(items=draft_items, status="pending")
    session.add(draft)
    await session.flush()

    return {
        "status": "ok",
        "draft_id": draft.id,
        "items": len(draft_items),
        "raw_prices_new": channel_new,
        "degraded": degraded,
        "reused": False,
        "approval_items_block": format_draft_lines(draft_items),
    }


async def notify_morning_price_draft(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    draft_id: int,
    items_block: str,
) -> None:
    """Send approval chat message after the draft row is committed."""
    approval_chats = await resolve_price_draft_destinations(session)
    if not approval_chats:
        logger.warning(
            "No PRICE_APPROVAL_CHAT_IDS / PRICE_APPROVAL_CHAT_ID / ADMIN_ALERT_CHAT_ID "
            "and no owners with dm_ok; draft {} not sent",
            draft_id,
        )
        return
    message = render_template(
        "price_draft",
        draft_id=draft_id,
        items_block=items_block,
    )
    markup = _price_draft_keyboard(draft_id)
    for chat_id in approval_chats:
        try:
            await telegram.send_message(int(chat_id), message, reply_markup=markup)
        except TelegramSendError as exc:
            logger.warning(
                "Failed to notify approval chat_id={} for draft_id={}: {}",
                chat_id,
                draft_id,
                exc,
            )


async def approve_price_draft(
    session: AsyncSession,
    *,
    draft_id: int,
    employee_id: int | None,
    telegram: TelegramClientProtocol,
    approver_telegram_id: int | None = None,
) -> str:
    """
    Atomically approve pending draft and publish to configured chats.

    Returns outcome: ``approved`` | ``approved_partial`` | ``not_found`` | ``already_processed``.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        text(
            """
            UPDATE price_list_drafts
            SET status = 'approved',
                approved_by = :employee_id,
                approved_by_telegram_id = :approver_telegram_id,
                approved_at = :approved_at
            WHERE id = :draft_id AND status = 'pending'
            RETURNING id, items
            """
        ),
        {
            "draft_id": draft_id,
            "employee_id": employee_id,
            "approver_telegram_id": approver_telegram_id,
            "approved_at": now,
        },
    )
    row = result.mappings().first()
    if row is None:
        existing = await session.get(PriceListDraft, draft_id)
        if existing is None:
            return "not_found"
        return "already_processed"

    items = row["items"] if isinstance(row["items"], list) else []
    publish_text = render_template(
        "price_list_published",
        items_block=format_draft_lines(items),
    )
    settings = get_settings()
    failed = 0
    for chat_id in settings.price_publish_chat_id_list:
        try:
            await telegram.send_message(int(chat_id), publish_text)
        except TelegramSendError as exc:
            logger.warning(
                "Failed to publish approved draft_id={} chat_id={}: {}",
                draft_id,
                chat_id,
                exc,
            )
            failed += 1
    return "approved_partial" if failed else "approved"


async def reject_price_draft(
    session: AsyncSession,
    *,
    draft_id: int,
    employee_id: int | None,
    approver_telegram_id: int | None = None,
) -> str:
    """Atomically reject pending draft. Returns outcome key."""
    now = datetime.now(UTC)
    result = await session.execute(
        text(
            """
            UPDATE price_list_drafts
            SET status = 'rejected',
                approved_by = :employee_id,
                approved_by_telegram_id = :approver_telegram_id,
                approved_at = :approved_at
            WHERE id = :draft_id AND status = 'pending'
            RETURNING id
            """
        ),
        {
            "draft_id": draft_id,
            "employee_id": employee_id,
            "approver_telegram_id": approver_telegram_id,
            "approved_at": now,
        },
    )
    row = result.first()
    if row is None:
        existing = await session.get(PriceListDraft, draft_id)
        if existing is None:
            return "not_found"
        return "already_processed"
    return "rejected"
