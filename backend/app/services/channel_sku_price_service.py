"""Проекция цен прайс-каналов в канонических ключах заявок.

``parsed_items.sku_key`` и ``requests.canonical_sku_key`` — разные пространства,
поэтому для оффера цены по заявке нужен отдельный слой: минимум дня считается
существующей агрегацией, а ``bridge_parsed_to_canonical`` переводит её строки в
ключ заявок. Строки без пары (не iPhone, неизвестная модель) в проекцию не
попадают — угадывать нечего.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.db.models import (
    ChannelSkuPrice,
    PriceSelectionStatus,
    Request,
    RequestPriceSelection,
    RequestPriceState,
    RequestStatus,
    Supplier,
)
from app.parsers.canonical_sku import NORMALIZER_VERSION, bridge_parsed_to_canonical
from app.services.daily_sku_price_service import (
    business_date_start,
    projection_row_is_fresh,
)
from app.services.markup_service import get_markup, load_rules
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import CallbackData, button, inline_keyboard

PRICE_REASON = "channel_price"
OFFER_STATUS = "proposed"
# Одна карточка на ценовой снимок: пересборка проекции повышает selection_version.
OFFER_KEY = "channel-price-offer:{request_id}:{price_id}:{price_version}"


def _offer_keyboard(request_id: int) -> dict[str, object]:
    return inline_keyboard(
        [
            [
                button(
                    "Отправить клиенту",
                    cd=CallbackData(namespace="chanprice", action="confirm", arg=request_id),
                ),
                button(
                    "Отмена",
                    cd=CallbackData(namespace="chanprice", action="dismiss", arg=request_id),
                ),
            ]
        ]
    )


def _observed_at(row: dict[str, Any], moment: datetime) -> datetime:
    post_date = row.get("source_post_date")
    if not isinstance(post_date, datetime):
        return moment
    return post_date if post_date.tzinfo else post_date.replace(tzinfo=UTC)


async def _channel_ids(session: AsyncSession, supplier_ids: set[int]) -> dict[int, int | None]:
    if not supplier_ids:
        return {}
    rows = await session.execute(
        select(Supplier.id, Supplier.price_channel_id).where(Supplier.id.in_(supplier_ids))
    )
    return {int(supplier_id): channel_id for supplier_id, channel_id in rows.all()}


async def rebuild_channel_sku_prices(
    session: AsyncSession,
    aggregated: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    invalidate_missing: bool = False,
) -> int:
    """Снимок агрегации дня/набора постов → строки ``channel_sku_prices``.

    Агрегацию передаёт вызывающий (``price_service``): SQL-минимум, RUB и
    отсечка low_confidence уже в ней, второй копии этих правил нет.
    """
    settings = get_settings()
    moment = now or datetime.now(UTC)
    rules = await load_rules(session)
    today = business_date_start(moment)
    projected: set[tuple[datetime, str, str]] = set()
    channel_by_supplier = await _channel_ids(
        session, {int(row["supplier_id"]) for row in aggregated if row.get("supplier_id")}
    )

    written = 0
    skipped = 0
    for row in aggregated:
        canonical_key = bridge_parsed_to_canonical(
            model=row.get("model"),
            storage=row.get("storage"),
            color=row.get("color"),
            region=row.get("region"),
            sim=row.get("sim"),
        )
        if canonical_key is None:
            skipped += 1
            continue
        purchase = Decimal(str(row["min_price"]))
        markup = get_markup(
            {"model": row.get("model"), "storage": row.get("storage")},
            rules,
            default=settings.default_markup,
        )
        if markup.is_fallback:
            # Прайс без утверждённой наценки владельцу не предлагаем: цены просто нет.
            skipped += 1
            continue
        observed = _observed_at(row, moment)
        biz_date = business_date_start(observed)
        expires = observed + timedelta(minutes=settings.channel_price_max_age_minutes)
        if biz_date != today or expires <= moment:
            skipped += 1
            continue
        values = {
            "business_date": biz_date,
            "canonical_sku_key": canonical_key,
            "sku_key": row["sku_key"],
            "model": row.get("model"),
            "storage": row.get("storage"),
            "color": row.get("color"),
            "region": row.get("region"),
            "sim": row.get("sim"),
            "purchase_unit_price": purchase,
            "client_unit_price": (purchase + markup.markup).quantize(Decimal("0.01")),
            "markup_rub": markup.markup,
            "markup_rule_id": markup.rule_id,
            "currency": row.get("currency") or "RUB",
            "supplier_id": int(row["supplier_id"]),
            "channel_id": channel_by_supplier.get(int(row["supplier_id"])),
            "parsed_item_id": row.get("parsed_item_id"),
            "observed_at": observed,
            "expires_at": expires,
            "normalizer_version": NORMALIZER_VERSION,
        }
        stmt = (
            pg_insert(ChannelSkuPrice)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["business_date", "canonical_sku_key", "currency"],
                set_={
                    **{key: values[key] for key in values if key != "business_date"},
                    "invalidated_at": None,
                    "selection_version": ChannelSkuPrice.selection_version + 1,
                },
                # Полный срез уже выбрал минимум. Более старый пост строку не
                # перетирает; равная или более новая дата обновляет цену и наценку.
                where=ChannelSkuPrice.observed_at <= observed,
            )
        )
        result = await session.execute(stmt)
        if result.rowcount:
            written += 1
            projected.add((biz_date, canonical_key, str(values["currency"])))
    if invalidate_missing:
        current = (
            await session.scalars(
                select(ChannelSkuPrice).where(
                    ChannelSkuPrice.business_date == today,
                    ChannelSkuPrice.invalidated_at.is_(None),
                )
            )
        ).all()
        for existing in current:
            identity = (existing.business_date, existing.canonical_sku_key, existing.currency)
            if identity not in projected:
                existing.invalidated_at = moment
    await session.flush()
    logger.info(
        "channel_sku_prices rebuilt: projected={} skipped={} day={}",
        written,
        skipped,
        today.date(),
    )
    return written


async def get_fresh_channel_price(
    session: AsyncSession,
    *,
    canonical_sku_key: str,
    now: datetime | None = None,
) -> ChannelSkuPrice | None:
    """Цена канала на сегодня, пока она не протухла и не отозвана."""
    if not get_settings().channel_sku_price_projection_enabled:
        return None
    moment = now or datetime.now(UTC)
    row = await session.scalar(
        select(ChannelSkuPrice).where(
            ChannelSkuPrice.business_date == business_date_start(moment),
            ChannelSkuPrice.canonical_sku_key == canonical_sku_key,
            ChannelSkuPrice.currency == "RUB",
            ChannelSkuPrice.invalidated_at.is_(None),
        )
    )
    if row is None or not projection_row_is_fresh(
        business_date=row.business_date,
        invalidated_at=row.invalidated_at,
        expires_at=row.expires_at,
        moment=moment,
    ):
        return None
    return row


async def offer_channel_price_for_request(
    session: AsyncSession,
    request: Request,
    *,
    telegram: TelegramClientProtocol,
    now: datetime | None = None,
) -> bool:
    """Показать сотрудникам цену из прайс-канала по заявке.

    Клиенту не уходит ничего: пишется только строка ``status='proposed'`` в
    ``request_price_selections`` и отправляется ephemeral-карточка в группу
    персонала. Публикация — исключительно по кнопке (см. confirm ниже).
    """
    if not get_settings().channel_price_offer_enabled:
        return False
    if not request.canonical_sku_key:
        return False
    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        return False
    if request.price_state in (RequestPriceState.published, RequestPriceState.published.value):
        return False

    moment = now or datetime.now(UTC)
    price = await get_fresh_channel_price(
        session, canonical_sku_key=request.canonical_sku_key, now=moment
    )
    if price is None:
        return False

    key = OFFER_KEY.format(
        request_id=request.id, price_id=price.id, price_version=price.selection_version
    )
    already = await session.scalar(
        select(RequestPriceSelection.id).where(RequestPriceSelection.idempotency_key == key)
    )
    if already is not None:
        return False

    from app.services.first_price_publication_service import _next_selection_version
    from app.services.staff_ephemeral_notify import notify_staff_ephemeral_in_group
    from app.templates.messages_ru import render_template

    session.add(
        RequestPriceSelection(
            request_id=request.id,
            batch_id=request.batch_id,
            canonical_sku_key=request.canonical_sku_key,
            status=OFFER_STATUS,
            selected_supplier_id=price.supplier_id,
            purchase_unit_price=price.purchase_unit_price,
            requested_qty=request.requested_qty,
            client_unit_price=price.client_unit_price,
            selection_reason=PRICE_REASON,
            candidate_count=1,
            selection_version=await _next_selection_version(session, request.id),
            selected_at=moment,
            idempotency_key=key,
        )
    )
    await session.flush()

    text = render_template(
        "channel_price_offer",
        request_id=request.id,
        sku_text=(request.source_text or "").strip()[:120] or "—",
        price=price.client_unit_price,
    )
    delivered = await notify_staff_ephemeral_in_group(
        session,
        telegram,
        group_chat_id=int(request.group_chat_id),
        text=text,
        request_id=request.id,
        reply_markup=_offer_keyboard(request.id),
    )
    if not delivered:
        logger.warning("channel_price_offer_not_delivered request_id={}", request.id)
    return bool(delivered)


async def offer_channel_price_safe(
    session: AsyncSession,
    request: Request,
    *,
    telegram: TelegramClientProtocol,
) -> None:
    """Оффер не должен ронять рассылку RFQ: сбой логируется, заявка идёт дальше."""
    if not get_settings().channel_price_offer_enabled:
        return
    try:
        await offer_channel_price_for_request(session, request, telegram=telegram)
    except Exception:
        logger.exception("channel_price_offer_failed request_id={}", request.id)


async def _latest_offer(session: AsyncSession, request_id: int) -> RequestPriceSelection | None:
    offer: RequestPriceSelection | None = await session.scalar(
        select(RequestPriceSelection)
        .where(
            RequestPriceSelection.request_id == request_id,
            RequestPriceSelection.status == OFFER_STATUS,
            RequestPriceSelection.selection_reason == PRICE_REASON,
        )
        .order_by(RequestPriceSelection.selection_version.desc())
        .limit(1)
    )
    return offer


async def _published_selection_id(session: AsyncSession, request_id: int) -> int | None:
    published_id: int | None = await session.scalar(
        select(RequestPriceSelection.id).where(
            RequestPriceSelection.request_id == request_id,
            RequestPriceSelection.status == PriceSelectionStatus.published.value,
        )
    )
    return published_id


async def confirm_channel_price_offer(
    session: AsyncSession,
    *,
    request_id: int,
    actor_employee_id: int | None = None,
    actor_telegram_id: int | None = None,
    now: datetime | None = None,
) -> str:
    """Отправить предложенную цену канала клиенту.

    Возвращает ``published`` / ``already`` / ``stale`` / ``closed`` / ``no_offer``.
    Цена перечитывается в момент нажатия: протухший или пересобранный снимок
    не уходит клиенту (fail-closed, в том числе при выключенном флаге проекции).
    """
    moment = now or datetime.now(UTC)
    offer = await _latest_offer(session, request_id)
    if offer is None:
        # Повторный клик: оффер уже опубликован либо отозван.
        return "already" if await _published_selection_id(session, request_id) else "no_offer"

    request = await session.get(Request, request_id)
    if request is None or request.status in (RequestStatus.closed, RequestStatus.cancelled):
        offer.status = PriceSelectionStatus.invalidated.value
        await session.flush()
        return "closed"

    published_id = await _published_selection_id(session, request_id)
    fresh = (
        await get_fresh_channel_price(session, canonical_sku_key=offer.canonical_sku_key, now=moment)
        if offer.canonical_sku_key
        else None
    )
    if published_id is not None or fresh is None or (
        fresh.client_unit_price != offer.client_unit_price
    ):
        offer.status = PriceSelectionStatus.superseded.value
        await session.flush()
        return "already" if published_id is not None else "stale"

    from app.services.first_price_publication_service import (
        enqueue_client_message,
        first_client_price_key,
        lock_price_selection,
    )
    from app.templates.messages_ru import render_template

    await lock_price_selection(session, request_id)
    if await _published_selection_id(session, request_id) is not None:
        offer.status = PriceSelectionStatus.superseded.value
        await session.flush()
        return "already"

    published_other = aliased(RequestPriceSelection)
    published_row_id = (
        await session.execute(
            update(RequestPriceSelection)
            .where(
                RequestPriceSelection.id == offer.id,
                RequestPriceSelection.status == OFFER_STATUS,
                ~exists(
                    select(published_other.id).where(
                        published_other.request_id == request_id,
                        published_other.status == PriceSelectionStatus.published.value,
                    )
                ),
            )
            .values(
                status=PriceSelectionStatus.published.value,
                published_at=moment,
                published_by=actor_employee_id,
                publication_reason=PRICE_REASON,
            )
            .returning(RequestPriceSelection.id)
        )
    ).scalar_one_or_none()
    if published_row_id is None:
        return "already"

    outbox_id = await enqueue_client_message(
        session,
        dedupe_key=first_client_price_key(request_id),
        chat_id=int(request.group_chat_id),
        text=render_template(
            "supplier_quote_parsed", request_id=request_id, price=offer.client_unit_price
        ),
        request_id=request_id,
    )
    await session.refresh(offer)
    offer.telegram_outbox_id = outbox_id
    request.price_state = RequestPriceState.published
    await session.flush()

    from app.services.audit_service import record_admin_mutation
    from app.services.telemetry import record_client_price_publication

    record_client_price_publication(PRICE_REASON)
    await record_admin_mutation(
        session,
        action="channel_price_published",
        entity_type="request",
        entity_id=str(request_id),
        actor_telegram_id=actor_telegram_id,
        previous_state={"offer_status": OFFER_STATUS},
        new_state={"client_unit_price": str(offer.client_unit_price)},
    )
    return "published"


async def dismiss_channel_price_offer(
    session: AsyncSession,
    *,
    request_id: int,
    actor_telegram_id: int | None = None,
) -> bool:
    """Снять оффер: клиенту ничего не отправляется, заявка ждёт поставщиков."""
    offer = await _latest_offer(session, request_id)
    if offer is None:
        return False
    offer.status = PriceSelectionStatus.invalidated.value
    await session.flush()
    from app.services.audit_service import record_admin_mutation

    await record_admin_mutation(
        session,
        action="channel_price_dismissed",
        entity_type="request",
        entity_id=str(request_id),
        actor_telegram_id=actor_telegram_id,
        previous_state={"offer_status": OFFER_STATUS},
        new_state={"offer_status": PriceSelectionStatus.invalidated.value},
    )
    return True
