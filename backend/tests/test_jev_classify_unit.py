"""Classifier short-circuits that do not need Postgres."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.config import get_settings
from app.db.models import Request, RequestStatus, Supplier
from app.integrations.jev.client import set_jev_client
from app.integrations.jev.exceptions import JevTimeout
from app.integrations.jev.models import JevCandidate, JevProviderResponse
from app.services.jev_supplier_classifier import (
    classify_with_jev,
    hash_candidate_set,
    hash_message_input,
)


class _Boom:
    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del message_text, candidates, correlation_id
        raise AssertionError("provider called")


class _Timeout:
    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del message_text, candidates, correlation_id
        raise JevTimeout()


def _supplier(**kwargs: object) -> Supplier:
    payload: dict[str, object] = {"id": 1, "name": "Supplier", "active": True, "telegram_id": 10}
    payload.update(kwargs)
    return Supplier(**payload)  # type: ignore[arg-type]


def _request() -> Request:
    return Request(
        id=1,
        status=RequestStatus.awaiting_answers,
        source_text="iPhone 17 Pro 256",
        normalized_json={"model": "iPhone 17 Pro", "storage": "256"},
        group_chat_id=1,
        employee_id=1,
    )


@pytest.fixture(autouse=True)
def _clear() -> None:
    yield
    set_jev_client(None)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()
    set_jev_client(_Boom())
    decision = await classify_with_jev(
        AsyncMock(),
        supplier=_supplier(),
        business_connection_id=None,
        chat_id=1,
        message_id=1,
        message_text="117900",
        candidates=[_request()],
    )
    assert decision.reason_code == "jev_disabled"
    assert decision.action.value == "fallback"


@pytest.mark.asyncio
async def test_no_candidates_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    get_settings.cache_clear()
    set_jev_client(_Boom())
    decision = await classify_with_jev(
        AsyncMock(),
        supplier=_supplier(),
        business_connection_id=None,
        chat_id=1,
        message_id=1,
        message_text="117900",
        candidates=[],
    )
    assert decision.action.value == "fallback"


@pytest.mark.asyncio
async def test_inactive_supplier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    get_settings.cache_clear()
    set_jev_client(_Boom())
    decision = await classify_with_jev(
        AsyncMock(),
        supplier=_supplier(active=False),
        business_connection_id=None,
        chat_id=1,
        message_id=1,
        message_text="117900",
        candidates=[_request()],
    )
    assert decision.reason_code == "supplier_inactive"


@pytest.mark.asyncio
async def test_timeout_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.setenv("JEV_MODEL", "jev-1.14.0")
    get_settings.cache_clear()
    set_jev_client(_Timeout())

    async def _no_cache(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.lookup_cached_decision",
        _no_cache,
    )

    async def _no_outbound(*args: object, **kwargs: object) -> dict[int, object]:
        del args, kwargs
        return {}

    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.load_jev_outbound_facts",
        _no_outbound,
    )
    decision = await classify_with_jev(
        AsyncMock(),
        supplier=_supplier(),
        business_connection_id="bc-1",
        chat_id=1,
        message_id=1,
        message_text="117900",
        candidates=[_request()],
    )
    assert decision.reason_code == "jev_timeout"
    assert decision.action.value == "fallback"


def test_cache_key_changes_with_text_and_status() -> None:
    first = hash_message_input("117900")
    edited = hash_message_input("118000")
    assert first != edited
    open_request = _request()
    closed = _request()
    closed.status = RequestStatus.closed
    assert hash_candidate_set([open_request]) != hash_candidate_set([closed])
    same_attrs = _request()
    same_attrs.source_text = "айфон 17 про 256 синий"
    assert hash_candidate_set([open_request]) != hash_candidate_set([same_attrs])


def test_candidate_state_keeps_order_text_and_sent_time() -> None:
    from datetime import UTC, datetime

    from app.services.jev_supplier_classifier import JevOutboundFact, candidates_to_jev

    request = _request()
    request.id = 42
    request.source_text = "айфон 17 про 256 синий"
    request.normalized_json = {
        "model": "iPhone 17 Pro",
        "storage": "256",
        "color": "Blue",
    }
    sent = datetime(2026, 10, 30, 9, 15, tzinfo=UTC)
    built = candidates_to_jev(
        [request],
        limit=20,
        outgoing={42: JevOutboundFact(sent_at=sent, kind="ask")},
    )
    candidate = built[0]
    assert candidate.order_text == "айфон 17 про 256 синий"
    assert "айфон 17 про 256 синий" in candidate.description
    assert "#42" in candidate.description
    assert "iPhone 17 Pro" in candidate.description
    assert candidate.outgoing_at == "2026-10-30T09:15:00+00:00"
    assert candidate.rfq_kind == "ask"
    assert candidate.model == "iPhone 17 Pro"
    from app.integrations.jev.prompts import build_questions, build_state

    state = build_state("117900 синий", built)
    assert state["candidates"][0]["order_text"] == "айфон 17 про 256 синий"
    assert state["candidates"][0]["outgoing_at"] == "2026-10-30T09:15:00+00:00"
    criteria = build_questions(built)["binding"]["criteria"]
    assert "айфон 17 про 256 синий" in criteria["request:42"]


def test_price_parser_keeps_thousands_suffix() -> None:
    from app.parsers.price_parse import parse_price_from_text

    parsed = parse_price_from_text("17 pro 256 blue 1sim 127.5к")
    assert parsed.price == Decimal("127500")
