"""Чистые функции диагностики: токен ошибки → причина → строка для экрана."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services import app_settings_service
from app.services.parser_client import ParserClientError, classify_parser_error
from app.services.parser_diagnostics import (
    _ALERT_KEY_PREFIX,
    Reason,
    diagnose_morning_price,
    format_last_alert_lines,
    format_reason_lines,
    last_alert_codes,
)
from app.services.price_service import IngestStats


@pytest.fixture
def parser_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PARSER_API_URL", "http://parser.test:8000")
    monkeypatch.setenv("PARSER_API_TOKEN", "test-parser-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("message", "token", "expected"),
    [
        ("parser_transport_error", "x", "unreachable"),
        ("parser_no_response", "x", "unreachable"),
        ("parser_http_401", "x", "auth"),
        ("parser_http_403", "x", "auth"),
        ("parser_http_401", "", "token_missing"),
        ("parser_http_503", "x", "http_error"),
        ("invalid response shape", "x", "bad_response"),
        ("Traceback (most recent call last)", "x", "unknown"),
    ],
)
def test_classify_parser_error(
    monkeypatch: pytest.MonkeyPatch,
    parser_env: None,
    message: str,
    token: str,
    expected: str,
) -> None:
    monkeypatch.setenv("PARSER_API_TOKEN", token)
    get_settings.cache_clear()

    assert classify_parser_error(ParserClientError(message)) == expected


def test_classify_reports_missing_url(parser_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # env_ignore_empty=True, поэтому пустой URL подменяем прямо в настройках.
    monkeypatch.setattr(get_settings(), "parser_api_url", "")

    assert classify_parser_error(ParserClientError("parser_api_url_missing")) == "url_missing"


def _diagnose(**overrides) -> list[Reason]:
    kwargs = {
        "stats": IngestStats(
            raw_new=0, degraded=0, channels_checked=1, posts_seen=2, posts_without_text=0
        ),
        "draft_items": 0,
        "aggregated_items": 0,
        "llm_configured": True,
        "unparsed_raw": 0,
        "low_confidence": 0,
        "confidence_threshold": 0.75,
    }
    kwargs.update(overrides)
    return diagnose_morning_price(**kwargs)


def test_draft_present_has_no_reasons() -> None:
    assert _diagnose(draft_items=3) == []


def test_faults_become_reasons() -> None:
    stats = IngestStats(
        raw_new=0,
        degraded=2,
        channels_checked=2,
        faults=("auth", "auth", "unreachable"),
    )

    codes = [reason.code for reason in _diagnose(stats=stats)]

    assert codes == ["parser_auth", "parser_unreachable"]


def test_no_channels_bound() -> None:
    stats = IngestStats(raw_new=0, degraded=0, channels_checked=0)

    assert [reason.code for reason in _diagnose(stats=stats)] == ["no_bound_channels"]


def test_empty_posts_and_textless_posts() -> None:
    empty = IngestStats(raw_new=0, degraded=0, channels_checked=1, posts_seen=0)
    assert [reason.code for reason in _diagnose(stats=empty)] == ["posts_empty"]

    media_only = IngestStats(
        raw_new=0,
        degraded=0,
        channels_checked=1,
        posts_seen=4,
        posts_without_text=4,
    )
    assert [reason.code for reason in _diagnose(stats=media_only)] == ["posts_without_text"]


def test_llm_and_confidence_reasons() -> None:
    codes = [
        reason.code
        for reason in _diagnose(llm_configured=False, unparsed_raw=7, low_confidence=3)
    ]

    assert codes == ["llm_not_configured", "all_low_confidence"]


def test_prices_without_markup_rule() -> None:
    assert [reason.code for reason in _diagnose(aggregated_items=5)] == ["all_dropped_by_markup"]


def test_fallback_reason_when_nothing_else_matches() -> None:
    assert [reason.code for reason in _diagnose()] == ["no_prices_today"]
    backlog = _diagnose(unparsed_raw=4)
    assert [reason.code for reason in backlog] == ["unparsed_backlog"]
    assert "4" in backlog[0].detail


def test_reason_lines_are_human_readable() -> None:
    lines = format_reason_lines([Reason("posts_empty"), Reason("parser_auth", "auth")])

    assert lines.startswith("• парсер не отдал ни одного поста")
    assert lines.endswith("• парсер отклонил токен (PARSER_API_TOKEN должен равняться API_AUTH_TOKEN) (auth)")
    assert format_reason_lines([]) == "• причина не определена"


@pytest.mark.asyncio
async def test_last_alert_codes_sorted_and_formattable(db_session: AsyncSession) -> None:
    await app_settings_service.set_setting(
        db_session, key=f"{_ALERT_KEY_PREFIX}posts_empty", value="not-a-date"
    )
    await app_settings_service.set_setting(
        db_session,
        key=f"{_ALERT_KEY_PREFIX}parser_auth",
        value="2000-01-01T00:00:00+00:00",
    )
    await app_settings_service.set_setting(
        db_session,
        key=f"{_ALERT_KEY_PREFIX}no_bound_channels",
        value=datetime.now(UTC).isoformat(),
    )
    await db_session.flush()

    codes = await last_alert_codes(db_session)

    assert codes == ["no_bound_channels"]
    assert "привязан к каналу прайса" in format_last_alert_lines(codes)
