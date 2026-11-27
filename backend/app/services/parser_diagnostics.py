"""Почему утренний прайс пуст: причины и громкий алерт.

Джоб ``morning-price`` завершался со ``status="ok"``, когда из парсера не пришло
ничего, поэтому в 11:00 в боте была тишина без единого сообщения об ошибке.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting
from app.services import app_settings_service
from app.services.alert_service import notify_operators
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template

if TYPE_CHECKING:
    from app.services.price_service import IngestStats

ALERT_DEDUPE_WINDOW = timedelta(minutes=20)
_ALERT_KEY_PREFIX = "morning_price_alert:"
# Экран «Парсер» показывает только свежие причины, иначе список превратится в архив.
_SCREEN_ALERT_WINDOW = timedelta(hours=24)
_SCREEN_ALERT_LIMIT = 5

# Токены classify_parser_error → коды причин.
_FAULT_REASONS = {
    "url_missing": "parser_url_missing",
    "token_missing": "parser_token_missing",
    "auth": "parser_auth",
    "unreachable": "parser_unreachable",
    "http_error": "parser_http_error",
    "bad_response": "parser_bad_response",
    "unknown": "parser_error",
}

REASONS: dict[str, str] = {
    "parser_url_missing": "PARSER_API_URL не задан — обращения к парсеру не было",
    "parser_token_missing": "PARSER_API_TOKEN пуст — парсер отвечает 401",
    "parser_auth": "парсер отклонил токен (PARSER_API_TOKEN должен равняться API_AUTH_TOKEN)",
    "parser_unreachable": "парсер не отвечает по PARSER_API_URL — проверь хост и порт 8000",
    "parser_http_error": "парсер вернул ошибку HTTP",
    "parser_bad_response": "парсер вернул не разобраннный ответ",
    "parser_error": "парсер вернул непредвиденную ошибку",
    "no_bound_channels": "ни один активный поставщик не привязан к каналу прайса",
    "price_channels_unbound": (
        "канал добавлен в парсер, но не привязан к поставщику — его цены не участвуют"
    ),
    "channel_data_stale": "канал привязан, но свежих постов сегодня не было",
    "non_rub_prices_dropped": "цены каналов не в RUB — в прайс они не попадают",
    "posts_empty": "парсер не отдал ни одного поста — канал пуст или завис в pending",
    "posts_without_text": "все посты канала без текста (фото/PDF) — такой прайс не разбирается",
    "llm_not_configured": "LLM не настроен — прайс из текста канала не извлечь",
    "all_low_confidence": "все цены отброшены как low_confidence",
    "all_dropped_by_markup": "цены есть, но фиксированной наценки нет ни у одного товара",
    "unparsed_backlog": "текст канала есть, но цены из него не извлечены",
    "no_prices_today": "за сегодня нет цен, которые можно собрать в прайс",
    "backfill_incomplete": "забор полной истории канала не завершился — прайс не собран",
}


@dataclass(frozen=True)
class Reason:
    """Код причины + человеческая строка для алерта и экрана «Парсер»."""

    code: str
    detail: str = ""

    def line(self) -> str:
        base = REASONS.get(self.code, self.code)
        return f"• {base} ({self.detail})" if self.detail else f"• {base}"


def format_reason_lines(reasons: list[Reason]) -> str:
    return "\n".join(reason.line() for reason in reasons) or "• причина не определена"


async def last_alert_codes(session: AsyncSession) -> list[str]:
    """Коды причин, о которых предупреждали в последние сутки (для экрана «Парсер»)."""
    result = await session.execute(
        select(AppSetting.key, AppSetting.value).where(
            AppSetting.key.startswith(_ALERT_KEY_PREFIX)
        )
    )
    now = datetime.now(UTC)
    fresh: list[tuple[str, datetime]] = []
    for key, value in result.all():
        try:
            sent_at = datetime.fromisoformat(value)
        except ValueError:
            continue
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        if now - sent_at < _SCREEN_ALERT_WINDOW:
            fresh.append((key.removeprefix(_ALERT_KEY_PREFIX), sent_at))
    fresh.sort(key=lambda item: item[1], reverse=True)
    return [code for code, _sent_at in fresh]


def format_last_alert_lines(codes: list[str]) -> str:
    return "\n".join(f"• {REASONS.get(code, code)}" for code in codes[:_SCREEN_ALERT_LIMIT])


def diagnose_morning_price(
    *,
    stats: IngestStats,
    draft_items: int,
    aggregated_items: int,
    llm_configured: bool,
    unparsed_raw: int,
    low_confidence: int,
    confidence_threshold: float,
    unbound_channels: int = 0,
    stale_channels: int = 0,
    non_rub_items: int = 0,
) -> list[Reason]:
    """Чистая функция: почему черновика нет."""
    if draft_items > 0:
        return []

    reasons: list[Reason] = []
    for fault in dict.fromkeys(stats.faults):
        reasons.append(Reason(_FAULT_REASONS.get(fault, "parser_error"), fault))

    if stats.channels_checked == 0:
        reasons.append(Reason("no_bound_channels"))
    elif not stats.faults and stats.posts_seen == 0:
        reasons.append(Reason("posts_empty"))
    elif stats.posts_without_text and stats.posts_without_text == stats.posts_seen:
        reasons.append(Reason("posts_without_text", f"{stats.posts_seen} шт."))

    if unbound_channels:
        reasons.append(Reason("price_channels_unbound", f"каналов: {unbound_channels}"))
    if stats.channels_checked and stale_channels == stats.channels_checked and not stats.faults:
        # При сломанном заборе «нет свежих постов» — шум: причину уже назвал сбой.
        reasons.append(Reason("channel_data_stale", f"каналов: {stale_channels}"))

    if not llm_configured and unparsed_raw > 0:
        reasons.append(Reason("llm_not_configured", f"ждут разбора: {unparsed_raw}"))
    if aggregated_items == 0 and low_confidence > 0:
        reasons.append(
            Reason("all_low_confidence", f"{low_confidence} строк, порог {confidence_threshold}")
        )
    if aggregated_items == 0 and non_rub_items > 0:
        reasons.append(Reason("non_rub_prices_dropped", f"{non_rub_items} строк не в RUB"))
    if aggregated_items > 0 and draft_items == 0:
        reasons.append(Reason("all_dropped_by_markup", f"товаров в агрегате: {aggregated_items}"))

    if not reasons:
        if unparsed_raw > 0:
            reasons.append(Reason("unparsed_backlog", f"ждут разбора: {unparsed_raw}"))
        else:
            reasons.append(Reason("no_prices_today"))

    return reasons


async def _alert_is_fresh(session: AsyncSession, code: str) -> bool:
    """Алерт по этой причине уже уходил в пределах окна дедупликации."""
    raw = await app_settings_service.get_setting(session, f"{_ALERT_KEY_PREFIX}{code}")
    if raw is None:
        return False
    try:
        sent_at = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Malformed alert dedupe stamp for {}: {}", code, raw)
        return False
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - sent_at < ALERT_DEDUPE_WINDOW


async def alert_morning_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    reasons: list[Reason],
) -> bool:
    """Отправить причины операторам и владельцам. Возвращает True, если письмо ушло."""

    pending = [
        reason for reason in reasons if not await _alert_is_fresh(session, reason.code)
    ]
    if not pending:
        return False

    delivered = await notify_operators(
        session,
        render_template("alert_morning_price_empty", reason_block=format_reason_lines(pending)),
        telegram=telegram,
    )
    if delivered < 1:
        return False
    stamp = datetime.now(UTC).isoformat()
    for reason in pending:
        await app_settings_service.set_setting(
            session,
            f"{_ALERT_KEY_PREFIX}{reason.code}",
            stamp,
        )
    await session.flush()
    return True
