"""Регресс «тихого ok»: пустой утренний прайс обязан назвать причину."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MarkupRule, ParsedItem, PriceListDraft, RawPrice, Supplier
from app.services.markup_service import invalidate_markup_rules_cache
from app.services.parser_client import ParserClientError
from app.services.price_service import build_morning_price
from tests.conftest import MockTelegramClient

ALERT_MARKER = "[ALERT] Утренний прайс не собран"


@pytest.fixture
def parser_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Детерминированные PARSER_*: иначе результат зависит от боевого .env."""
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    monkeypatch.setenv("PARSER_API_TOKEN", "test-parser-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fail_posts(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    async def _fail(**_kwargs):
        raise ParserClientError(message)
        yield  # pragma: no cover

    monkeypatch.setattr("app.services.price_service.iter_posts", _fail)


def _empty_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(**_kwargs):
        if False:  # pragma: no cover
            yield []

    monkeypatch.setattr("app.services.price_service.iter_posts", _none)


def _alerts(telegram: MockTelegramClient) -> list[str]:
    return [text for _chat, text, _markup in telegram.sent if ALERT_MARKER in text]


@pytest.mark.asyncio
async def test_parser_401_alerts_auth_reason(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
    parser_env: None,
) -> None:
    _fail_posts(monkeypatch, "parser_http_401")

    result = await build_morning_price(db_session, mock_telegram)

    assert result["status"] == "ok"
    assert "parser_auth" in result["reason"]
    alerts = _alerts(mock_telegram)
    assert alerts
    assert all("парсер отклонил токен" in text for text in alerts)


@pytest.mark.asyncio
async def test_empty_token_is_not_reported_as_auth(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
    parser_env: None,
) -> None:
    monkeypatch.setenv("PARSER_API_TOKEN", "")
    get_settings.cache_clear()
    _fail_posts(monkeypatch, "parser_http_401")

    result = await build_morning_price(db_session, mock_telegram)

    assert result["reason"] == ["parser_token_missing"]
    assert "PARSER_API_TOKEN пуст" in _alerts(mock_telegram)[0]


@pytest.mark.asyncio
async def test_unreachable_parser_alerts(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
    parser_env: None,
) -> None:
    _fail_posts(monkeypatch, "parser_transport_error")

    result = await build_morning_price(db_session, mock_telegram)

    assert result["reason"] == ["parser_unreachable"]
    assert "порт 8000" in _alerts(mock_telegram)[0]


@pytest.mark.asyncio
async def test_no_bound_channels_alerts(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    parser_env: None,
) -> None:
    result = await build_morning_price(db_session, mock_telegram)

    assert result["reason"] == ["no_bound_channels"]
    assert "не привязан к каналу прайса" in _alerts(mock_telegram)[0]


@pytest.mark.asyncio
async def test_alert_is_deduped_within_window(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
    parser_env: None,
) -> None:
    _fail_posts(monkeypatch, "parser_http_401")

    first = await build_morning_price(db_session, mock_telegram)
    sent_after_first = len(_alerts(mock_telegram))
    second = await build_morning_price(db_session, mock_telegram)

    assert first["reason"] == second["reason"] == ["parser_auth"]
    assert sent_after_first > 0
    assert len(_alerts(mock_telegram)) == sent_after_first


@pytest.mark.asyncio
async def test_draft_present_sends_no_alert(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
    parser_env: None,
) -> None:
    _empty_posts(monkeypatch)
    db_session.add(
        MarkupRule(
            category="iphone_16",
            rule_key="test_iphone_16_alerts",
            model_pattern=r"iphone 16",
            markup_fixed=Decimal("800"),
            priority=50,
            active=True,
        )
    )
    raw = RawPrice(
        supplier_id=seed_channel_supplier.id,
        text="iPhone 16 256 80000",
        content_hash="test-alerts-iphone-16",
        source="channel_post",
        received_at=datetime.now(UTC),
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        ParsedItem(
            raw_price_id=raw.id,
            sku_key="iphone-16-256",
            model="iPhone 16",
            storage="256",
            price=Decimal("80000.00"),
            currency="RUB",
        )
    )
    await db_session.flush()
    invalidate_markup_rules_cache()

    result = await build_morning_price(db_session, mock_telegram)

    assert result["items"] == 1
    assert result["draft_id"] is not None
    assert "reason" not in result
    assert _alerts(mock_telegram) == []
    drafts = await db_session.scalar(select(func.count()).select_from(PriceListDraft))
    assert drafts == 1


@pytest.mark.asyncio
async def test_failed_delivery_does_not_stamp_dedupe(
    db_session: AsyncSession,
    seed_channel_supplier: Supplier,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
    parser_env: None,
) -> None:
    from app.db.models import AppSetting
    from app.telegram.client import TelegramSendError

    _fail_posts(monkeypatch, "parser_http_401")
    original = mock_telegram.send_message
    fail_send = True

    async def _maybe(*args, **kwargs):
        if fail_send:
            raise TelegramSendError("down")
        return await original(*args, **kwargs)

    monkeypatch.setattr(mock_telegram, "send_message", _maybe)

    first = await build_morning_price(db_session, mock_telegram)
    stamps = (
        await db_session.scalars(
            select(AppSetting.key).where(AppSetting.key.like("morning_price_alert:%"))
        )
    ).all()

    assert first["reason"] == ["parser_auth"]
    assert stamps == []

    fail_send = False
    second = await build_morning_price(db_session, mock_telegram)

    assert second["reason"] == ["parser_auth"]
    assert _alerts(mock_telegram)
