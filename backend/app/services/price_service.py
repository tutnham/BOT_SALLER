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
from sqlalchemy import func, or_, select, text, update
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
    TelegramOutbox,
    TelegramOutboxStatus,
)
from app.llm.client import LLMProviderError, get_llm_client
from app.llm.schemas import (
    ParsedPriceItem,
    ParsedPriceList,
    validate_price_list_payload,
)
from app.parsers.cache import get_cached, normalize_input_text, set_cached
from app.parsers.product_normalizer import (
    canonicalize_catalog_fields,
    extract_product_attrs,
    sku_key_fields_strict,
)
from app.services.audit_service import record_audit
from app.services.markup_service import (
    MARKUP_CATEGORIES,
    ensure_markup_rule_for_product,
    get_markup,
    invalidate_markup_rules_cache,
    load_rules,
    markup_rule_label,
    retire_shadowed_custom_rules,
)
from app.services.ops_status_service import llm_configuration_snapshot
from app.services.parser_client import (
    MANUAL_CHANNEL_HISTORY_LIMIT,
    ParserClientError,
    ParserPost,
    classify_parser_error,
    get_latest_posts,
    iter_posts,
    list_channels,
)
from app.services.parser_diagnostics import alert_morning_price, diagnose_morning_price
from app.services.publish_channel_service import resolve_publish_targets
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.telegram.keyboards import CallbackData, button, inline_keyboard, menu_button
from app.templates.messages_ru import render_template
from app.utils.html import TELEGRAM_HTML_PARSE_MODE, sanitize_telegram_html

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


class LatestIngestResult(NamedTuple):
    stats: IngestStats
    raw_price_ids: tuple[int, ...]


MANUAL_PRICE_LATEST_POSTS = MANUAL_CHANNEL_HISTORY_LIMIT
_LAST_MESSAGES_JOIN = "\n\n---\n\n"


def combine_latest_post_texts(
    posts: list[ParserPost],
) -> tuple[str, datetime | None, int | None, str | None]:
    """Склеить тексты последних постов канала (от старых к новым) для одного вызова LLM."""
    ordered = sorted(
        posts,
        key=lambda p: (_as_utc(p.post_date), int(p.post_id)),
    )
    blocks: list[str] = []
    newest_date: datetime | None = None
    newest_post_id: int | None = None
    newest_link: str | None = None
    for post in ordered:
        text_body = (post.raw_text or "").strip()
        if not text_body:
            continue
        blocks.append(text_body)
        post_date = _as_utc(post.post_date)
        if newest_date is None or post_date >= newest_date:
            newest_date = post_date
            newest_post_id = int(post.post_id)
            newest_link = post.message_link
    if not blocks:
        return "", None, None, None
    return _LAST_MESSAGES_JOIN.join(blocks), newest_date, newest_post_id, newest_link


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


def canonical_sku_key(
    *,
    model: str | None,
    storage: str | None,
    color: str | None,
    region: str | None,
    sim: str | None,
    strict: bool = False,
) -> str:
    """sku_key with the canonical product name, same as new parsed_items rows.

    Rows materialized before canonicalization keep raw-model keys; recomputing
    with this helper merges them with post-deploy rows of the same product.
    ``strict`` additionally normalizes the key tail for every brand, so one
    Samsung position from two channels collapses into one sku_key.
    """
    if strict:
        return build_sku_key(
            sku_key_fields_strict(
                model=model, storage=storage, color=color, region=region, sim=sim
            )
        )
    canonical_model, canonical_storage = canonicalize_catalog_fields(model, storage)
    return build_sku_key(
        {
            "model": canonical_model,
            "storage": canonical_storage,
            "color": color,
            "region": region,
            "sim": sim,
        }
    )


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


def format_rub_amount(amount: Decimal | str | int) -> str:
    """47000.00 -> «47 000 ₽»."""
    value = Decimal(str(amount))
    if value == value.to_integral_value():
        body = f"{int(value):,}".replace(",", " ")
    else:
        body = f"{value.quantize(Decimal('0.01')):,.2f}".replace(",", " ")
    return f"{body} ₽"


def present_parser_title(
    *,
    model: str | None,
    storage: str | None,
    color: str | None,
    region: str | None,
    sim: str | None,
) -> str:
    canonical, parsed_storage = canonicalize_catalog_fields(model, storage)
    return format_sku_title(
        model=canonical or model,
        storage=storage or parsed_storage,
        color=color,
        region=region,
        sim=sim,
    )


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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
        if source_post_id is not None:
            await session.execute(
                update(ParsedItem)
                .where(
                    ParsedItem.raw_price_id.in_(
                        select(RawPrice.id).where(
                            RawPrice.supplier_id == supplier_id,
                            RawPrice.source_post_id == source_post_id,
                            RawPrice.id != new_id,
                        )
                    )
                )
                .values(condition="superseded:post_edit")
            )
        raw = await session.get(RawPrice, new_id)
        return raw, True

    existing_result = await session.execute(
        select(RawPrice)
        .where(RawPrice.content_hash == content_hash)
        .options(selectinload(RawPrice.parsed_items))
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None and post_date is not None and existing.post_date is None:
        existing.post_date = _as_utc(post_date)
        await session.flush()
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
    write_lock = asyncio.Lock()

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
                                post_date = _as_utc(post.post_date)
                                if max_post_date is None or post_date > max_post_date:
                                    max_post_date = post_date
                                async with write_lock:
                                    raw, is_new = await insert_raw_price(
                                        session,
                                        supplier_id=supplier.id,
                                        text=text_body,
                                        source="channel_post",
                                        source_post_id=post.post_id,
                                        source_message_link=post.message_link,
                                        post_date=post_date,
                                    )
                                    if is_new and raw is not None:
                                        raw_new += 1
                    except ParserClientError as exc:
                        fetch_error = exc
                if max_post_date is not None:
                    cursor = supplier.last_price_sync_at
                    if cursor is not None and cursor.tzinfo is None:
                        cursor = cursor.replace(tzinfo=UTC)
                    if cursor is None or max_post_date > cursor:
                        async with write_lock:
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


async def ingest_latest_channel_prices(
    session: AsyncSession,
    *,
    latest: int = MANUAL_PRICE_LATEST_POSTS,
) -> LatestIngestResult:
    """Последние N постов канала: тексты склеиваются, один raw на поставщика для LLM."""
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
    raw_price_ids: list[int] = []
    faults: list[str] = []
    degraded = 0

    for supplier in suppliers:
        channel_id = supplier.price_channel_id
        if channel_id is None:
            continue
        try:
            posts = await get_latest_posts(channel_id=int(channel_id), latest=latest)
        except ParserClientError as exc:
            degraded += 1
            faults.append(classify_parser_error(exc))
            logger.warning(
                "Parser unavailable for supplier_id={} channel_id={}: {}",
                supplier.id,
                channel_id,
                exc,
            )
            continue

        posts_seen += len(posts)
        combined, post_date, source_post_id, message_link = combine_latest_post_texts(
            posts
        )
        if not combined:
            posts_without_text += len(posts)
            continue
        posts_without_text += sum(
            1 for post in posts if not (post.raw_text or "").strip()
        )
        raw, is_new = await insert_raw_price(
            session,
            supplier_id=supplier.id,
            text=combined,
            source="channel_last_messages",
            source_post_id=source_post_id,
            source_message_link=message_link,
            post_date=post_date,
        )
        if raw is not None:
            raw_price_ids.append(int(raw.id))
        if is_new and raw is not None:
            raw_new += 1

        if post_date is not None:
            cursor = supplier.last_price_sync_at
            if cursor is not None and cursor.tzinfo is None:
                cursor = cursor.replace(tzinfo=UTC)
            if cursor is None or post_date > cursor:
                supplier.last_price_sync_at = post_date
                await session.flush()

    stats = IngestStats(
        raw_new=raw_new,
        degraded=degraded,
        channels_checked=len(suppliers),
        posts_seen=posts_seen,
        posts_without_text=posts_without_text,
        faults=tuple(faults),
    )
    return LatestIngestResult(stats=stats, raw_price_ids=tuple(raw_price_ids))


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
        model, storage = canonicalize_catalog_fields(item.model, item.storage)
        session.add(
            ParsedItem(
                raw_price_id=raw.id,
                sku_key=canonical_sku_key(
                    model=model,
                    storage=storage,
                    color=item.color,
                    region=item.region,
                    sim=item.sim,
                    strict=True,
                ),
                model=model,
                storage=storage,
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


async def parse_raw_prices_for_ids(
    session: AsyncSession,
    raw_price_ids: list[int] | tuple[int, ...],
) -> int:
    """Разобрать только указанные raw_prices (без сканирования всей истории)."""
    if not raw_price_ids:
        return 0
    settings = get_settings()
    threshold = settings.confidence_threshold
    concurrency = max(1, int(settings.morning_price_llm_concurrency))
    inserted = 0
    result = await session.execute(
        select(RawPrice)
        .where(RawPrice.id.in_(tuple(raw_price_ids)))
        .where(~RawPrice.parsed_items.any())
        .options(selectinload(RawPrice.parsed_items))
        .order_by(RawPrice.id.asc())
    )
    batch = list(result.scalars().all())
    if not batch:
        return 0

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
    await session.commit()
    return inserted


_AGGREGATE_ORDERS = {
    "cheapest": "pi.price ASC, pi.id ASC",
    "newest": "rp.post_date DESC NULLS LAST, rp.id DESC, pi.id DESC",
}


def _aggregate_rows_sql(*, scope_sql: str, mode: str) -> str:
    """Один sku_key — одна строка: победитель и второе предложение.

    Побеждает минимальная цена (заказчик: «100 против 110 → берём 100») либо
    самый свежий пост, где нужна история по дате. Канал победителя и альтернатива
    возвращаются, чтобы владелец видел, из какого прайса взята цена.
    """
    order_sql = _AGGREGATE_ORDERS[mode]
    return f"""
        WITH ranked_items AS (
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
                rp.id AS raw_price_id,
                rp.supplier_id,
                rp.post_date AS source_post_date,
                rp.source_message_link,
                s.name AS supplier_name,
                s.price_channel_username,
                ROW_NUMBER() OVER (
                    PARTITION BY pi.sku_key, pi.currency
                    ORDER BY {order_sql}
                ) AS rn
            FROM parsed_items pi
            JOIN raw_prices rp ON rp.id = pi.raw_price_id
            JOIN suppliers s ON s.id = rp.supplier_id
            WHERE pi.currency = 'RUB'
              AND COALESCE(pi.condition, '') NOT LIKE 'low_confidence:%'
              AND COALESCE(pi.condition, '') NOT LIKE 'superseded:%'
              AND {scope_sql}
        )
        SELECT
            w.sku_key,
            w.id AS parsed_item_id,
            w.model,
            w.storage,
            w.color,
            w.region,
            w.sim,
            w.price AS min_price,
            w.currency,
            w.raw_price_id,
            w.supplier_id,
            w.supplier_name,
            w.price_channel_username,
            w.source_post_date,
            w.source_message_link,
            alt.price AS alt_price,
            alt.supplier_name AS alt_supplier_name,
            alt.price_channel_username AS alt_price_channel_username
        FROM ranked_items w
        LEFT JOIN ranked_items alt
            ON alt.sku_key = w.sku_key AND alt.currency = w.currency AND alt.rn = 2
        WHERE w.rn = 1
        ORDER BY w.sku_key
    """


async def aggregate_today(session: AsyncSession) -> list[dict[str, Any]]:
    """
    Min-price aggregation for today's parsed items (D2/D3).

    Groups by ``(sku_key, currency)``; draft path uses RUB only.
    """
    day_start = _today_start_utc(get_settings().tz)
    sql = text(_aggregate_rows_sql(scope_sql="rp.received_at >= :day_start", mode="cheapest"))
    result = await session.execute(sql, {"day_start": day_start})
    return [dict(row) for row in result.mappings().all()]


async def aggregate_latest(session: AsyncSession) -> list[dict[str, Any]]:
    """
    Full-history aggregation: newest post wins per ``(sku_key, currency)``.

    Same row shape as ``aggregate_today`` (``price AS min_price``); RUB only.
    """
    sql = text(_aggregate_rows_sql(scope_sql="TRUE", mode="newest"))
    result = await session.execute(sql)
    return [dict(row) for row in result.mappings().all()]


async def aggregate_from_raw_price_ids(
    session: AsyncSession,
    raw_price_ids: list[int] | tuple[int, ...],
    *,
    mode: str = "newest",
) -> list[dict[str, Any]]:
    """Цена по sku только среди переданных raw_prices (ручной прайс).

    ``cheapest`` отдаёт минимум набора — то же, что видит утренний прогон и чего
    просит заказчик; ``newest`` оставляет поведение «канал поправил цену».
    """
    if not raw_price_ids:
        return []
    sql = text(
        _aggregate_rows_sql(scope_sql="rp.id = ANY(:raw_price_ids)", mode=mode)
    )
    result = await session.execute(
        sql,
        {"raw_price_ids": list(raw_price_ids)},
    )
    return [dict(row) for row in result.mappings().all()]


def _row_channel_label(
    row: dict[str, Any],
    *,
    name_key: str,
    username_key: str,
) -> str | None:
    """Название канала-источника цены: @username, без него — имя поставщика."""
    handle = str(row.get(username_key) or "").strip()
    if handle and not handle.startswith("@"):
        handle = f"@{handle}"
    return handle or str(row.get(name_key) or "").strip() or None


def build_draft_items(
    aggregated: list[dict[str, Any]],
    rules: list[MarkupRule],
    default_markup: Decimal,
) -> list[dict[str, Any]]:
    """Build public-safe draft items with markup applied.

    Только публичные поля: закуп, канал и наценку владелец видит из
    ``review_sources`` (см. ``build_review_sources``).
    """
    draft_items: list[dict[str, Any]] = []
    for row in aggregated:
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


def _amount(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def build_review_sources(
    aggregated: list[dict[str, Any]],
    draft_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Откуда взялась каждая строка черновика: канал минимума, закуп, альтернатива.

    Хранится отдельно от ``items``: черновик остаётся публично-безопасным, а
    владелец по этим данным проверяет, что выбран именно минимум между каналами.
    """
    pending = {item["sku_key"]: item for item in draft_items}
    review: list[dict[str, Any]] = []
    for row in aggregated:
        item = pending.pop(row.get("sku_key"), None)
        if item is None:
            continue
        purchase = _amount(row.get("min_price"))
        our_price = item.get("our_price")
        review.append(
            {
                "sku_key": row.get("sku_key"),
                "title": item.get("title"),
                "our_price": our_price,
                "channel": _row_channel_label(
                    row, name_key="supplier_name", username_key="price_channel_username"
                ),
                "purchase": purchase,
                "alt_channel": _row_channel_label(
                    row,
                    name_key="alt_supplier_name",
                    username_key="alt_price_channel_username",
                ),
                "alt_purchase": _amount(row.get("alt_price")),
                "markup": (
                    _amount(Decimal(str(our_price)) - Decimal(purchase))
                    if our_price is not None and purchase is not None
                    else None
                ),
            }
        )
    return review


def build_full_history_draft_items(
    aggregated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Manual full-history draft: supplier price as-is, no markup."""
    draft_items: list[dict[str, Any]] = []
    for row in aggregated:
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


async def count_unparsed_raw_prices_all(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count(RawPrice.id)).where(~RawPrice.parsed_items.any())
    )
    return int(result.scalar_one())


async def count_low_confidence_items_all(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count(ParsedItem.id)).where(
            ParsedItem.condition.like("low_confidence:%")
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


async def count_unbound_price_channels(session: AsyncSession) -> int:
    """Каналы реестра парсера, не привязанные ни к одному поставщику.

    Без привязки у поста нет ``supplier_id``, поэтому такие цены молча не
    участвуют в прайсе — владельцу нужен счётчик, а не тишина.
    """
    try:
        channels = await list_channels()
    except Exception as exc:  # проба не имеет права ронять алерт вместо причины
        logger.warning("Price channel registry probe failed: {}", exc)
        return 0
    result = await session.execute(
        select(Supplier.price_channel_id).where(Supplier.price_channel_id.is_not(None))
    )
    bound = {int(value) for value in result.scalars().all() if value is not None}
    return sum(
        1
        for ch in channels
        if ch.is_active
        and ch.status == "active"
        and ch.channel_id is not None
        and int(ch.channel_id) not in bound
    )


async def count_stale_bound_channels(session: AsyncSession) -> int:
    """Привязанные каналы, откуда не было свежих постов за сегодня."""
    day_start = _today_start_utc(get_settings().tz)
    result = await session.execute(
        select(func.count(Supplier.id)).where(
            Supplier.active.is_(True),
            Supplier.price_channel_id.is_not(None),
            or_(
                Supplier.last_price_sync_at.is_(None),
                Supplier.last_price_sync_at < day_start,
            ),
        )
    )
    return int(result.scalar_one())


async def count_non_rub_items(
    session: AsyncSession,
    *,
    raw_price_ids: list[int] | tuple[int, ...] | None = None,
) -> int:
    """Позиции не в RUB в окне прогона. Агрегатор их не берёт, счётчик остаётся видимым."""
    stmt = (
        select(func.count(ParsedItem.id))
        .join(RawPrice, RawPrice.id == ParsedItem.raw_price_id)
        .where(ParsedItem.currency != "RUB")
    )
    if raw_price_ids is not None:
        stmt = stmt.where(RawPrice.id.in_(list(raw_price_ids)))
    else:
        stmt = stmt.where(RawPrice.received_at >= _today_start_utc(get_settings().tz))
    return int((await session.execute(stmt)).scalar_one())


async def count_non_rub_items_today(session: AsyncSession) -> int:
    """Сегодняшние позиции не в RUB: агрегатор их отбрасывает."""
    return await count_non_rub_items(session)


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
    await retire_shadowed_custom_rules(session)
    fresh = await load_rules(session)
    if fresh:
        rules = fresh
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


def format_review_sources_lines(review: list[dict[str, Any]] | None) -> str:
    """Строка владельцу: цена плюс канал, из которого пришёл минимум, и вторая цена.

    В публичный прайс закупы каналов не попадают — он идёт через
    ``format_published_price_html``.
    """
    if not review:
        return "—"
    lines: list[str] = []
    for row in review:
        title = row.get("title") or row.get("sku_key") or "—"
        line = f"{title} — {format_rub_amount(row.get('our_price') or 0)}"
        notes: list[str] = []
        channel = row.get("channel")
        if channel and row.get("purchase") is not None:
            notes.append(f"{channel}: закуп {format_rub_amount(row['purchase'])}")
        alt = row.get("alt_channel")
        if alt and alt != channel and row.get("alt_purchase") is not None:
            notes.append(f"{alt} — {format_rub_amount(row['alt_purchase'])}")
        if row.get("markup") is not None:
            notes.append(f"+{format_rub_amount(row['markup'])}")
        if notes:
            line = f"{line} ({'; '.join(notes)})"
        lines.append(line)
    return "\n".join(lines)


PRICE_PUBLISH_KIND = "price_publish"
# Порядок групп публичного прайса = порядок категорий дерева наценок.
_PUBLISH_GROUPS: tuple[tuple[str, str], ...] = tuple(
    (code, label) for _arg, label, code in MARKUP_CATEGORIES
)
_PUBLISH_GROUP_CODES = frozenset(code for code, _label in _PUBLISH_GROUPS)


def price_publish_dedupe_key(draft_id: int, chat_id: int) -> str:
    return f"{PRICE_PUBLISH_KIND}:{draft_id}:{chat_id}"


def _publish_group_code(title: str) -> str:
    """Категория строки прайса — тот же normalizer, что у правил наценок."""
    family = (extract_product_attrs(title).family or "").lower()
    return family if family in _PUBLISH_GROUP_CODES else "other"


def format_published_price_html(items: list[dict[str, Any]]) -> str:
    """Публичный прайс для канала: группы по категориям и суммы «47 000 ₽».

    Закуп, наценку и имя поставщика сюда не кладём (test_price_visibility_rules);
    title приходит из текста канала, поэтому экранируется перед parse_mode=HTML.
    """
    grouped: dict[str, list[str]] = {}
    for item in items:
        title = str(item.get("title") or item.get("sku_key") or "").strip()
        price = item.get("our_price")
        if not title or price is None:
            continue
        grouped.setdefault(_publish_group_code(title), []).append(
            f"{sanitize_telegram_html(title)} — {format_rub_amount(price)}"
        )
    blocks = [
        f"<b>{label}</b>\n" + "\n".join(grouped[code])
        for code, label in _PUBLISH_GROUPS
        if grouped.get(code)
    ]
    return "\n\n".join(blocks)


def _publish_date_label(moment: datetime, tz_name: str) -> str:
    """Дата прайса по бизнес-дню (``settings.tz``), а не по UTC."""
    return moment.astimezone(ZoneInfo(tz_name)).strftime("%d.%m.%Y")


async def _enqueue_price_publish(
    session: AsyncSession, *, draft_id: int, chat_id: int, text: str
) -> bool:
    """Одна строка ``telegram_outbox`` на пару (черновик, канал).

    ``dedupe_key`` уникален, поэтому повторное утверждение того же черновика не
    может поставить в очередь второе сообщение.
    """
    inserted = (
        await session.execute(
            insert(TelegramOutbox)
            .values(
                dedupe_key=price_publish_dedupe_key(draft_id, chat_id),
                chat_id=chat_id,
                request_id=None,
                kind=PRICE_PUBLISH_KIND,
                text=text,
                parse_mode=TELEGRAM_HTML_PARSE_MODE,
                status=TelegramOutboxStatus.pending.value,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(TelegramOutbox.id)
        )
    ).scalar_one_or_none()
    return inserted is not None


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
        label = markup_rule_label(rule)
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


async def _draft_or_alert(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    result: dict[str, Any],
    stats: IngestStats,
    draft_items: list[dict[str, Any]],
    review_sources: list[dict[str, Any]],
    aggregated_items: int,
    unparsed_raw: int,
    low_confidence: int,
    raw_price_ids: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Один финал для крона и ручной кнопки: черновик либо алерт с причиной.

    Пустой прогон обязан оставить причину (no_silent_failure): без черновика
    владелец получает `[ALERT]` с кодом, а не тишину.
    """
    settings = get_settings()
    non_rub_items = await count_non_rub_items(session, raw_price_ids=raw_price_ids)
    if non_rub_items:
        logger.warning("non-RUB parsed items dropped count={}", non_rub_items)
    if not draft_items:
        reasons = diagnose_morning_price(
            stats=stats,
            draft_items=0,
            aggregated_items=aggregated_items,
            llm_configured=bool(llm_configuration_snapshot()["configured"]),
            unparsed_raw=unparsed_raw,
            low_confidence=low_confidence,
            confidence_threshold=settings.confidence_threshold,
            unbound_channels=await count_unbound_price_channels(session),
            stale_channels=await count_stale_bound_channels(session),
            non_rub_items=non_rub_items,
        )
        result["reason"] = [reason.code for reason in reasons]
        if reasons:
            await alert_morning_price(session, telegram, reasons)
        # markup_prompt_rule_ids уже в result: владелец получит вопрос о наценке
        # и без черновика (jobs.run_morning_price читает их отдельно).
        return result

    existing = await _find_identical_pending_draft(session, draft_items)
    if existing is not None:
        result["draft_id"] = existing.id
        result["reused"] = True
        # Старые снимки со статусом stored ещё никому не показывали.
        if existing.status == "stored":
            existing.status = "pending"
            result["notify_draft"] = True
        # Тот же прайс мог собраться из других каналов: происхождение обновляем.
        existing.review_sources = review_sources
    else:
        draft = PriceListDraft(
            items=draft_items, review_sources=review_sources, status="pending"
        )
        session.add(draft)
        await session.flush()
        result["draft_id"] = draft.id
        result["reused"] = False
        result["notify_draft"] = True
    return result


async def _project_channel_prices(
    session: AsyncSession, aggregated: list[dict[str, Any]]
) -> None:
    """Снимок цен каналов в канонических ключах — источник оффера по заявке.

    Флаг выключен → ничего не происходит. Сбой проекции не отменяет черновик:
    savepoint откатывает только её, причина остаётся в логах.
    """
    if not get_settings().channel_sku_price_projection_enabled:
        return
    from app.services.channel_sku_price_service import rebuild_channel_sku_prices

    try:
        async with session.begin_nested():
            await rebuild_channel_sku_prices(session, aggregated, invalidate_missing=True)
    except Exception:
        logger.exception("channel_sku_price_projection_failed")


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
    await _project_channel_prices(session, aggregated)

    result: dict[str, Any] = {
        "status": "ok",
        "draft_id": None,
        "items": len(draft_items),
        "raw_prices_new": stats.raw_new,
        "degraded": stats.degraded,
        "markup_prompt_rule_ids": [int(rule.id) for rule in unknown_rules],
        "notify_draft": False,
    }
    return await _draft_or_alert(
        session,
        telegram,
        result=result,
        stats=stats,
        draft_items=draft_items,
        review_sources=build_review_sources(aggregated, draft_items),
        aggregated_items=len(aggregated),
        unparsed_raw=await count_unparsed_raw_prices_today(session),
        low_confidence=await count_low_confidence_items_today(session),
    )


async def build_full_history_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    """Ручная кнопка: последние N постов каждого канала, минимум по sku и наценка.

    Минимум берётся между каналами; внутри канала пост уже склеен в один
    raw_price, поэтому старая цена не перекрывает исправленную.
    MANUAL_PRICE_USE_MARKUP=false даёт прежний вид: новейший пост и цена без наценки.
    """
    settings = get_settings()
    ingest = await ingest_latest_channel_prices(session, latest=MANUAL_PRICE_LATEST_POSTS)
    stats = ingest.stats
    await parse_raw_prices_for_ids(session, ingest.raw_price_ids)
    with_markup = settings.manual_price_use_markup
    aggregated = await aggregate_from_raw_price_ids(
        session,
        ingest.raw_price_ids,
        mode="cheapest" if with_markup else "newest",
    )
    unknown_rules: list[MarkupRule] = []
    review_sources: list[dict[str, Any]] = []
    if with_markup:
        rules = await load_rules(session)
        unknown_rules = await collect_unknown_markup_products(session, aggregated, rules)
        rules = await load_rules(session)
        draft_items = build_draft_items(aggregated, rules, settings.default_markup)
        review_sources = build_review_sources(aggregated, draft_items)
        # mode="cheapest": снимок совпадает с тем, что видит утренний прогон.
        await _project_channel_prices(session, aggregated)
    else:
        draft_items = build_full_history_draft_items(aggregated)

    result: dict[str, Any] = {
        "status": "ok",
        "draft_id": None,
        "items": len(draft_items),
        "raw_prices_new": stats.raw_new,
        "degraded": stats.degraded,
        "markup_prompt_rule_ids": [int(rule.id) for rule in unknown_rules],
        "notify_draft": False,
    }
    return await _draft_or_alert(
        session,
        telegram,
        result=result,
        stats=stats,
        draft_items=draft_items,
        review_sources=review_sources,
        aggregated_items=len(aggregated),
        unparsed_raw=await count_unparsed_raw_prices_all(session),
        low_confidence=await count_low_confidence_items_all(session),
        raw_price_ids=ingest.raw_price_ids,
    )


async def notify_morning_price_draft(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    draft_id: int,
    items_block: str,
    review_block: str | None = None,
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
    review_message = (
        render_template("price_draft_review", draft_id=draft_id, items_block=review_block)
        if review_block
        else None
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
            continue
        if review_message is None:
            continue
        try:
            await telegram.send_message(int(chat_id), review_message)
        except TelegramSendError as exc:
            logger.warning(
                "Failed to send price provenance chat_id={} for draft_id={}: {}",
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
    Atomically approve pending draft and publish to configured channels.

    Channels come from the ``price_publish_channels`` registry (env seeded as
    fallback). Returns ``approved`` | ``approved_partial`` | ``approved_queued``
    | ``not_found`` | ``already_processed`` | ``delivery_unavailable``.
    """
    now = datetime.now(UTC)
    settings = get_settings()
    if settings.price_publish_via_outbox and not settings.client_price_inline_delivery:
        from app.services.heartbeat_service import (
            heartbeat_is_fresh,
            latest_heartbeat_at,
        )

        if not heartbeat_is_fresh(await latest_heartbeat_at(session, "worker")):
            return "delivery_unavailable"
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
    settings = get_settings()
    publish_text = render_template(
        "price_list_published_html",
        date=_publish_date_label(now, settings.tz),
        groups_block=format_published_price_html(items),
    )
    targets = await resolve_publish_targets(session)
    if not targets:
        logger.warning("approved draft_id={} has no publish channels", draft_id)
        from app.services.alert_service import notify_operators

        await notify_operators(
            session,
            f"⚠️ Прайс #{draft_id} утверждён, но каналов публикации нет.\n"
            "Добавьте их в меню «Публикация прайса».",
            telegram=telegram,
        )
    if settings.price_publish_via_outbox:
        # Доставка — за worker'ом: сбой уходит в ретрай/DLQ, а не теряется
        # в момент нажатия (история OPS-008).
        queued = 0
        for chat_id in targets:
            if await _enqueue_price_publish(
                session, draft_id=draft_id, chat_id=int(chat_id), text=publish_text
            ):
                queued += 1
        outcome = "approved_queued" if queued else "approved"
    else:
        failed = 0
        for chat_id in targets:
            try:
                await telegram.send_message(
                    int(chat_id),
                    publish_text,
                    parse_mode=TELEGRAM_HTML_PARSE_MODE,
                )
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
        new_state={
            "status": outcome,
            "channels": len(targets),
            "via_outbox": settings.price_publish_via_outbox,
        },
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
