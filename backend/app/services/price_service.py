"""Morning price pipeline (TECH DOC §9.5)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import (
    MarkupRule,
    Owner,
    ParsedItem,
    PriceListDraft,
    RawPrice,
    Supplier,
)
from app.llm.client import LLMProviderError, get_llm_client
from app.llm.schemas import (
    ParsedPriceItem,
    ParsedPriceList,
    validate_price_list_payload,
)
from app.parsers.cache import get_cached, normalize_input_text, set_cached
from app.services.audit_service import record_audit
from app.services.markup_service import (
    ensure_markup_rule_for_product,
    get_markup,
    invalidate_markup_rules_cache,
    load_rules,
)
from app.services.ops_status_service import llm_configuration_snapshot
from app.services.parser_client import (
    ParserClientError,
    classify_parser_error,
    iter_posts,
)
from app.services.parser_diagnostics import alert_morning_price, diagnose_morning_price
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.telegram.keyboards import CallbackData, button, inline_keyboard, menu_button
from app.templates.messages_ru import render_template

CHANNEL_PRICE_CONTENT_TYPES = ("text", "photo", "document", "media_group")
RAW_PRICE_PARSE_BATCH = 200


class IngestStats(NamedTuple):
    """Итоги выгрузки из парсера. Поля 0/1 совпадают со старым ``tuple[int, int]``."""

    raw_new: int
    degraded: int
    channels_checked: int = 0
    posts_seen: int = 0
    posts_without_text: int = 0
    faults: tuple[str, ...] = ()


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


def channel_price_sync_from(supplier: Supplier, tz_name: str) -> datetime:
    """Incremental cursor, or start of today so old history stays out of the draft."""
    if supplier.last_price_sync_at is not None:
        return supplier.last_price_sync_at
    return _today_start_utc(tz_name)


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
    post_date: datetime | None = None,
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
            post_date=post_date,
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
    *,
    full_history: bool = False,
) -> IngestStats:
    """
    Fetch channel posts for suppliers with ``price_channel_id``.

    Помимо счётчиков возвращает причины, по которым данных нет: утренний джоб
    не имеет права завершаться «ok» молча.
    """
    result = await session.execute(
        select(Supplier).where(
            Supplier.active.is_(True),
            Supplier.price_channel_id.is_not(None),
        )
    )
    suppliers = list(result.scalars().all())
    raw_new = 0
    posts_seen = 0
    posts_without_text = 0

    semaphore = asyncio.Semaphore(5)

    async def _ingest_supplier_channel(supplier: Supplier) -> tuple[Supplier, Exception | None]:
        nonlocal raw_new, posts_seen, posts_without_text
        channel_id = supplier.price_channel_id
        if channel_id is None:
            return supplier, None
        async with semaphore:
            try:
                settings = get_settings()
                from_ = None if full_history else channel_price_sync_from(supplier, settings.tz)
                max_post_date: datetime | None = None
                fetch_error: Exception | None = None
                for content_type in CHANNEL_PRICE_CONTENT_TYPES:
                    try:
                        async for page in iter_posts(
                            channel_id=int(channel_id),
                            from_=from_,
                            content_type=content_type,
                            page_size=500 if full_history else 100,
                        ):
                            for post in page:
                                posts_seen += 1
                                text_body = (post.raw_text or "").strip()
                                if not text_body:
                                    posts_without_text += 1
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
                                    post_date=post.post_date,
                                )
                                if is_new and raw is not None:
                                    raw_new += 1
                    except ParserClientError as exc:
                        fetch_error = exc
                if max_post_date is not None and (
                    supplier.last_price_sync_at is None
                    or max_post_date > supplier.last_price_sync_at
                ):
                    supplier.last_price_sync_at = max_post_date
                    await session.flush()
                return supplier, fetch_error
            except ParserClientError as exc:
                return supplier, exc

    fetch_results = await asyncio.gather(
        *[_ingest_supplier_channel(supplier) for supplier in suppliers]
    )

    degraded = 0
    faults: list[str] = []
    for supplier, fetch_error in fetch_results:
        channel_id = supplier.price_channel_id
        if channel_id is None:
            continue
        if fetch_error is not None:
            degraded += 1
            faults.append(classify_parser_error(fetch_error))
            logger.warning(
                "Parser unavailable for supplier_id={} channel_id={}: {}",
                supplier.id,
                channel_id,
                fetch_error,
            )

    return IngestStats(
        raw_new=raw_new,
        degraded=degraded,
        channels_checked=len(suppliers),
        posts_seen=posts_seen,
        posts_without_text=posts_without_text,
        faults=tuple(faults),
    )


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


async def _materialize_parsed_items(
    session: AsyncSession,
    raw: RawPrice,
    parsed: ParsedPriceList,
    *,
    threshold: float,
) -> int:
    inserted = 0
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
    if inserted:
        await session.flush()
    return inserted


async def parse_pending_raw_prices(
    session: AsyncSession,
    *,
    include_history: bool = False,
) -> int:
    """
    Parse today's raw_prices that still lack parsed_items.

    D2: low-confidence items are stored with a condition tag and skipped in aggregation.
    Returns number of newly materialised parsed_items rows.
    """
    settings = get_settings()
    day_start = _today_start_utc(settings.tz)
    threshold = settings.confidence_threshold
    concurrency = max(1, int(settings.morning_price_llm_concurrency))
    inserted = 0
    last_id = 0

    while True:
        conditions = [
            RawPrice.id > last_id,
            ~RawPrice.parsed_items.any(),
        ]
        if not include_history:
            conditions.append(RawPrice.received_at >= day_start)
        result = await session.execute(
            select(RawPrice)
            .where(*conditions)
            .options(selectinload(RawPrice.parsed_items))
            .order_by(RawPrice.id.asc())
            .limit(RAW_PRICE_PARSE_BATCH)
        )
        batch = list(result.scalars().all())
        if not batch:
            break
        last_id = int(batch[-1].id)

        cached_pairs: list[tuple[RawPrice, ParsedPriceList]] = []
        pending_llm: list[RawPrice] = []
        for raw in batch:
            cached = await get_cached(session, kind="price_list", raw_text=raw.text)
            if cached is not None:
                parsed = validate_price_list_payload(cached)
                if parsed is not None:
                    cached_pairs.append((raw, parsed))
                continue
            pending_llm.append(raw)

        semaphore = asyncio.Semaphore(concurrency)

        async def _parse_uncached(
            raw: RawPrice, semaphore: asyncio.Semaphore = semaphore
        ) -> tuple[RawPrice, ParsedPriceList | None]:
            async with semaphore:
                llm = get_llm_client()
                try:
                    parsed = await llm.parse_price_list(raw.text)
                except LLMProviderError as exc:
                    logger.warning("LLM parse_price_list failed: {}", exc)
                    return raw, None
                return raw, parsed

        llm_pairs = await asyncio.gather(*[_parse_uncached(raw) for raw in pending_llm])
        for raw, llm_parsed in llm_pairs:
            if llm_parsed is None:
                continue
            await set_cached(
                session,
                kind="price_list",
                raw_text=raw.text,
                result_json=llm_parsed.model_dump(mode="json"),
                model_used=settings.llm_model or settings.llm_provider,
            )
            cached_pairs.append((raw, llm_parsed))

        for raw, parsed in cached_pairs:
            inserted += await _materialize_parsed_items(
                session, raw, parsed, threshold=threshold
            )

        if include_history:
            # Таймаут полного прогона не должен терять уже оплаченную работу LLM.
            await session.commit()

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


async def aggregate_latest(session: AsyncSession) -> list[dict[str, Any]]:
    """
    Full-history aggregation: newest post wins per ``(sku_key, currency)``.

    Same row shape as ``aggregate_today`` (``price AS min_price``); RUB only.
    """
    sql = text(
        """
        WITH latest_items AS (
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
                    ORDER BY rp.post_date DESC NULLS LAST, rp.id DESC, pi.id DESC
                ) AS rn
            FROM parsed_items pi
            JOIN raw_prices rp ON rp.id = pi.raw_price_id
            WHERE pi.currency = 'RUB'
              AND COALESCE(pi.condition, '') NOT LIKE 'low_confidence:%'
        )
        SELECT sku_key, model, storage, color, region, sim, price AS min_price, currency
        FROM latest_items
        WHERE rn = 1
        ORDER BY sku_key
        """
    )
    result = await session.execute(sql)
    return [dict(row) for row in result.mappings().all()]


def build_draft_items(
    aggregated: list[dict[str, Any]],
    rules: list[MarkupRule],
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

        min_price = Decimal(str(row["min_price"]))
        product = {
            "model": row.get("model"),
            "storage": row.get("storage"),
        }
        markup = get_markup(product, rules, default=default_markup)
        if markup.is_fallback:
            continue
        our_price = min_price + markup.markup
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


def build_full_history_draft_items(
    aggregated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Manual full-history draft: supplier price as-is, no markup."""
    draft_items: list[dict[str, Any]] = []
    for row in aggregated:
        currency = row.get("currency") or "RUB"
        if currency != "RUB":
            continue
        price = Decimal(str(row["min_price"])).quantize(Decimal("0.01"))
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
                "our_price": str(price),
                "currency": "RUB",
            }
        )
    return draft_items


async def count_unparsed_raw_prices_today(session: AsyncSession) -> int:
    """Сегодняшние raw_prices без parsed_items — сколько ждёт разбора LLM."""
    day_start = _today_start_utc(get_settings().tz)
    result = await session.execute(
        select(func.count(RawPrice.id)).where(
            RawPrice.received_at >= day_start,
            ~RawPrice.parsed_items.any(),
        )
    )
    return int(result.scalar_one())


async def count_low_confidence_items_today(session: AsyncSession) -> int:
    """Сегодняшние parsed_items, отброшенные агрегатором из-за low confidence."""
    day_start = _today_start_utc(get_settings().tz)
    result = await session.execute(
        select(func.count(ParsedItem.id))
        .join(RawPrice, RawPrice.id == ParsedItem.raw_price_id)
        .where(
            RawPrice.received_at >= day_start,
            ParsedItem.condition.like("low_confidence:%"),
        )
    )
    return int(result.scalar_one())


def _product_markup_phrase(row: dict[str, Any]) -> str:
    return format_sku_title(
        model=row.get("model"),
        storage=row.get("storage"),
        color=None,
        region=None,
        sim=None,
    )


async def collect_unknown_markup_products(
    session: AsyncSession,
    aggregated: list[dict[str, Any]],
    rules: list[MarkupRule],
) -> list[MarkupRule]:
    """Create markup rules for parsed products that have no fixed markup yet."""
    settings = get_settings()
    day_start = _today_start_utc(settings.tz)
    now = datetime.now(UTC)
    prompted: list[MarkupRule] = []
    seen_keys: set[str] = set()
    for row in aggregated:
        product = {"model": row.get("model"), "storage": row.get("storage")}
        if not get_markup(product, rules, default=settings.default_markup).is_fallback:
            continue
        phrase = _product_markup_phrase(row)
        if not phrase or phrase == "—":
            continue
        try:
            rule = await ensure_markup_rule_for_product(session, phrase=phrase)
        except ValueError:
            continue
        if not rule.rule_key or rule.rule_key in seen_keys:
            continue
        seen_keys.add(rule.rule_key)
        if rule.markup_fixed is not None:
            continue
        if rule.updated_at is not None and rule.updated_at >= day_start:
            continue
        rule.updated_at = now
        prompted.append(rule)
    if prompted:
        await session.flush()
        invalidate_markup_rules_cache()
    return prompted


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
            PriceListDraft.status.in_(("pending", "stored")),
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


def _new_product_markup_keyboard(rule_id: int) -> dict[str, Any]:
    return inline_keyboard(
        [
            [
                menu_button(str(preset), "mk_set", rule_id, page=preset)
                for preset in (500, 800, 1000)
            ],
            [menu_button("Своя сумма", "mk_custom", rule_id)],
            [menu_button("Не добавлять", "mk_skip", rule_id)],
        ]
    )


async def notify_unknown_markup_rules(
    telegram: TelegramClientProtocol,
    *,
    chat_ids: list[int],
    rules: list[MarkupRule],
) -> None:
    """Ask owners in bot DM which markup to use for newly parsed products."""
    for rule in rules:
        label = (rule.rule_key or rule.category or "товар").replace("_", " ")
        if label.startswith("custom "):
            label = label[len("custom ") :]
        message = render_template("markup_new_product_prompt", rule_label=label)
        markup = _new_product_markup_keyboard(int(rule.id))
        for chat_id in chat_ids:
            try:
                await telegram.send_message(int(chat_id), message, reply_markup=markup)
            except TelegramSendError as exc:
                logger.warning(
                    "Failed to ask markup for rule_id={} chat_id={}: {}",
                    rule.id,
                    chat_id,
                    exc,
                )


async def build_morning_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    """Ingest channel prices, parse, and queue a pending draft for owner approval."""
    settings = get_settings()
    stats = await ingest_channel_prices(session)
    await parse_pending_raw_prices(session)
    aggregated = await aggregate_today(session)
    rules = await load_rules(session)
    unknown_rules = await collect_unknown_markup_products(session, aggregated, rules)
    rules = await load_rules(session)
    draft_items = build_draft_items(aggregated, rules, settings.default_markup)

    result: dict[str, Any] = {
        "status": "ok",
        "draft_id": None,
        "items": len(draft_items),
        "raw_prices_new": stats.raw_new,
        "degraded": stats.degraded,
        "markup_prompt_rule_ids": [int(rule.id) for rule in unknown_rules],
        "notify_draft": False,
    }

    if not draft_items:
        reasons = diagnose_morning_price(
            stats=stats,
            draft_items=0,
            aggregated_items=len(aggregated),
            llm_configured=bool(llm_configuration_snapshot()["configured"]),
            unparsed_raw=await count_unparsed_raw_prices_today(session),
            low_confidence=await count_low_confidence_items_today(session),
            confidence_threshold=settings.confidence_threshold,
        )
        result["reason"] = [reason.code for reason in reasons]
        if reasons:
            await alert_morning_price(session, telegram, reasons)

    if not draft_items and not unknown_rules:
        return result

    if draft_items:
        existing = await _find_identical_pending_draft(session, draft_items)
        if existing is not None:
            result["draft_id"] = existing.id
            result["reused"] = True
            # Старые снимки со статусом stored ещё никому не показывали.
            if existing.status == "stored":
                existing.status = "pending"
                result["notify_draft"] = True
        else:
            draft = PriceListDraft(items=draft_items, status="pending")
            session.add(draft)
            await session.flush()
            result["draft_id"] = draft.id
            result["reused"] = False
            result["notify_draft"] = True
    return result


async def build_full_history_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    """Manual button: full channel history, newest price per sku, no markup."""
    settings = get_settings()
    stats = await ingest_channel_prices(session, full_history=True)
    await parse_pending_raw_prices(session, include_history=True)
    aggregated = await aggregate_latest(session)
    draft_items = build_full_history_draft_items(aggregated)

    result: dict[str, Any] = {
        "status": "ok",
        "draft_id": None,
        "items": len(draft_items),
        "raw_prices_new": stats.raw_new,
        "degraded": stats.degraded,
        "markup_prompt_rule_ids": [],
        "notify_draft": False,
    }

    if not draft_items:
        reasons = diagnose_morning_price(
            stats=stats,
            draft_items=0,
            aggregated_items=len(aggregated),
            llm_configured=bool(llm_configuration_snapshot()["configured"]),
            unparsed_raw=await count_unparsed_raw_prices_today(session),
            low_confidence=await count_low_confidence_items_today(session),
            confidence_threshold=settings.confidence_threshold,
        )
        result["reason"] = [reason.code for reason in reasons]
        if reasons:
            await alert_morning_price(session, telegram, reasons)
        return result

    existing = await _find_identical_pending_draft(session, draft_items)
    if existing is not None:
        result["draft_id"] = existing.id
        result["reused"] = True
        if existing.status == "stored":
            existing.status = "pending"
            result["notify_draft"] = True
    else:
        draft = PriceListDraft(items=draft_items, status="pending")
        session.add(draft)
        await session.flush()
        result["draft_id"] = draft.id
        result["reused"] = False
        result["notify_draft"] = True
    return result


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
    outcome = "approved_partial" if failed else "approved"
    await record_audit(
        session,
        action="price_draft_approve",
        entity_type="price_list_draft",
        entity_id=str(draft_id),
        actor_telegram_id=approver_telegram_id,
        new_state={"status": outcome},
    )
    return outcome


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
    await record_audit(
        session,
        action="price_draft_reject",
        entity_type="price_list_draft",
        entity_id=str(draft_id),
        actor_telegram_id=approver_telegram_id,
        new_state={"status": "rejected"},
    )
    return "rejected"
