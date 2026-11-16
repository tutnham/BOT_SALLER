"""classify_lines_with_jev: chunking, legacy fallback, cache, breaker. No Postgres."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.config import get_settings
from app.db.models import Request, RequestStatus, Supplier
from app.integrations.jev.client import set_jev_client
from app.integrations.jev.exceptions import JevInvalidResponse, JevTimeout
from app.integrations.jev.models import (
    JevCandidate,
    JevDecisionAction,
    JevMessageClass,
    JevProviderResponse,
    JevSupplierDecision,
)
from app.integrations.jev.prompts import SCHEMA_VERSION
from app.services import jev_breaker_service as breaker
from app.services.jev_supplier_classifier import classify_lines_with_jev


def _supplier() -> Supplier:
    return Supplier(id=1, name="Supplier", active=True, telegram_id=10)


def _request() -> Request:
    return Request(
        id=11,
        status=RequestStatus.awaiting_answers,
        source_text="iPhone 17 Pro 256 Blue",
        normalized_json={"model": "iPhone 17 Pro", "storage": "256", "color": "Blue"},
        group_chat_id=1,
        employee_id=1,
    )


def _response(choice: str = "request:11") -> JevProviderResponse:
    return JevProviderResponse(
        model="jev-1.14.0",
        message_choice="price_offer",
        price_probability=Decimal("0.99"),
        binding_choice=choice,
        binding_probabilities={choice: Decimal("0.99"), "none": Decimal("0.01")},
        binding_confidence=Decimal("0.98"),
        matches_listed_order=Decimal("0.9"),
    )


class _BatchClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.chunks: list[dict[int, str]] = []
        self.fail = fail

    async def classify_supplier_lines(
        self,
        *,
        lines: dict[int, str],
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> dict[int, JevProviderResponse | JevInvalidResponse]:
        del candidates, correlation_id
        self.chunks.append(dict(lines))
        if self.fail:
            raise JevTimeout()
        return {line_no: _response() for line_no in lines}


class _LegacyClient:
    """Adapter without the batch method: must fall back to sequential calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del message_text, candidates, correlation_id
        self.calls += 1
        return _response()


class _BoomBatch:
    async def classify_supplier_lines(
        self,
        *,
        lines: dict[int, str],
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> dict[int, JevProviderResponse]:
        del lines, candidates, correlation_id
        raise AssertionError("provider called while breaker open")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.setenv("JEV_SHADOW_MODE", "true")
    get_settings.cache_clear()

    async def _no_cache(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _no_persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _no_outbound(*args: object, **kwargs: object) -> dict[int, object]:
        del args, kwargs
        return {}

    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.lookup_cached_decision", _no_cache
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.persist_jev_decision", _no_persist
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.load_jev_outbound_facts", _no_outbound
    )
    yield
    set_jev_client(None)
    get_settings.cache_clear()


async def _classify(lines: list[tuple[int, str]]) -> dict[int, JevSupplierDecision]:
    return await classify_lines_with_jev(
        AsyncMock(),
        supplier=_supplier(),
        business_connection_id=None,
        chat_id=1,
        message_in_id=5,
        lines=lines,
        candidates=[_request()],
    )


@pytest.mark.asyncio
async def test_chunking_respects_batch_max_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_BATCH_MAX_LINES", "2")
    get_settings.cache_clear()
    client = _BatchClient()
    set_jev_client(client)
    lines = [(no, f"iPhone 17 Pro 256 Blue 11790{no}") for no in range(1, 6)]
    results = await _classify(lines)
    assert [len(chunk) for chunk in client.chunks] == [2, 2, 1]
    assert set(results) == {1, 2, 3, 4, 5}
    for decision in results.values():
        assert decision.action is JevDecisionAction.AUTO_BIND
        assert decision.selected_candidate_id == 11
        assert decision.reason_code == "jev_price_and_binding_accepted"


@pytest.mark.asyncio
async def test_legacy_client_falls_back_to_sequential_calls() -> None:
    client = _LegacyClient()
    set_jev_client(client)
    lines = [(1, "iPhone 17 Pro 256 Blue 117900"), (2, "iPhone 17 Pro 256 Blue 118000")]
    results = await _classify(lines)
    assert client.calls == 2
    assert set(results) == {1, 2}
    assert all(d.action is JevDecisionAction.AUTO_BIND for d in results.values())


@pytest.mark.asyncio
async def test_breaker_open_skips_provider_for_all_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEV_BREAKER_FAILURE_STREAK", "1")
    get_settings.cache_clear()
    set_jev_client(_BoomBatch())
    await breaker.note_jev_breaker_failure(get_settings())
    results = await _classify([(1, "117900"), (2, "68000")])
    for decision in results.values():
        assert decision.reason_code == "jev_breaker_open"
        assert decision.error_code == "jev_breaker_open"
        assert decision.action is JevDecisionAction.FALLBACK


@pytest.mark.asyncio
async def test_batch_transport_error_fails_all_lines_in_chunk() -> None:
    client = _BatchClient(fail=True)
    set_jev_client(client)
    results = await _classify([(1, "117900"), (2, "68000")])
    assert len(client.chunks) == 1
    for decision in results.values():
        assert decision.error_code == "jev_timeout"
        assert decision.action is JevDecisionAction.FALLBACK
    assert breaker.jev_breaker_snapshot()["failure_streak"] == 1


@pytest.mark.asyncio
async def test_invalid_line_answer_falls_back_only_that_line() -> None:
    class _PartialBatch(_BatchClient):
        async def classify_supplier_lines(
            self,
            *,
            lines: dict[int, str],
            candidates: list[JevCandidate],
            correlation_id: str,
        ) -> dict[int, JevProviderResponse | JevInvalidResponse]:
            answers = await super().classify_supplier_lines(
                lines=lines, candidates=candidates, correlation_id=correlation_id
            )
            answers[2] = JevInvalidResponse()
            return answers

    set_jev_client(_PartialBatch())
    results = await _classify([(1, "iPhone 17 Pro 256 Blue 117900"), (2, "68000")])
    assert results[1].action is JevDecisionAction.AUTO_BIND
    assert results[2].error_code == "jev_invalid_response"
    assert results[2].action is JevDecisionAction.FALLBACK


@pytest.mark.asyncio
async def test_cached_decisions_skip_provider_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = JevSupplierDecision(
        message_class=JevMessageClass.PRICE_OFFER,
        price_probability=Decimal("0.99"),
        confidence=Decimal("0.98"),
        action=JevDecisionAction.AUTO_BIND,
        reason_code="jev_price_and_binding_accepted",
        model="jev-1.14.0",
        schema_version=SCHEMA_VERSION,
        selected_candidate_id=11,
    )

    async def _cached(*args: object, **kwargs: object) -> JevSupplierDecision:
        del args, kwargs
        return cached

    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.lookup_cached_decision", _cached
    )
    client = _BatchClient()
    set_jev_client(client)
    results = await _classify([(1, "117900"), (2, "68000")])
    assert client.chunks == []
    for decision in results.values():
        assert decision.from_cache is True
        assert decision.action is JevDecisionAction.AUTO_BIND