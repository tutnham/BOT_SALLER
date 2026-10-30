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
    candidates_to_jev,
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
    with_qty = _request()
    with_qty.requested_qty = 5
    assert hash_candidate_set([open_request]) != hash_candidate_set([with_qty])
    str_qty = _request()
    str_qty.normalized_json = {"model": "iPhone 17 Pro", "storage": "256", "qty": "3"}
    assert candidates_to_jev([str_qty], limit=5)[0].quantity == 3
    bool_qty = _request()
    bool_qty.normalized_json = {"model": "iPhone 17 Pro", "storage": "256", "qty": True}
    assert candidates_to_jev([bool_qty], limit=5)[0].quantity is None


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


@pytest.mark.asyncio
async def test_classify_slices_candidates_before_hash_and_outbound_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from app.services.candidate_loader_service import JevOutboundFact

    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.setenv("JEV_MAX_CANDIDATES", "2")
    get_settings.cache_clear()

    loaded_ids: list[int] = []
    cached_hashes: list[str] = []

    async def _mock_outbound(*args: object, **kwargs: object) -> dict[int, JevOutboundFact]:
        del args
        req_ids = list(kwargs.get("request_ids") or [])  # type: ignore[arg-type]
        loaded_ids.extend(req_ids)
        return {
            rid: JevOutboundFact(sent_at=datetime(2026, 10, 30, 9, 0, tzinfo=UTC), kind="ask")
            for rid in req_ids
        }

    async def _mock_cache(*args: object, **kwargs: object) -> None:
        del args
        cached_hashes.append(str(kwargs.get("candidate_set_hash")))
        return None

    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.load_jev_outbound_facts",
        _mock_outbound,
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.lookup_cached_decision",
        _mock_cache,
    )
    set_jev_client(_Timeout())

    r1 = _request()
    r1.id = 1
    r1.source_text = "old request 1"
    r2 = _request()
    r2.id = 2
    r3 = _request()
    r3.id = 3

    await classify_with_jev(
        AsyncMock(),
        supplier=_supplier(),
        business_connection_id=None,
        chat_id=1,
        message_id=1,
        message_text="117900",
        candidates=[r1, r2, r3],
    )
    assert sorted(loaded_ids) == [2, 3]

    # Mutating r1 (outside top-2 limit) must NOT change candidate_set_hash
    r1_mutated = _request()
    r1_mutated.id = 1
    r1_mutated.source_text = "mutated old request 1"
    await classify_with_jev(
        AsyncMock(),
        supplier=_supplier(),
        business_connection_id=None,
        chat_id=1,
        message_id=1,
        message_text="117900",
        candidates=[r1_mutated, r2, r3],
    )
    assert len(cached_hashes) == 2
    assert cached_hashes[0] == cached_hashes[1]


@pytest.mark.asyncio
async def test_classify_accepts_preloaded_outgoing_without_db_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from app.services.candidate_loader_service import JevOutboundFact

    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    get_settings.cache_clear()

    async def _forbid_outbound(*args: object, **kwargs: object) -> dict[int, JevOutboundFact]:
        del args, kwargs
        raise AssertionError("load_jev_outbound_facts should not be called when outgoing is passed")

    async def _no_cache(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.load_jev_outbound_facts",
        _forbid_outbound,
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.lookup_cached_decision",
        _no_cache,
    )
    set_jev_client(_Timeout())

    r1 = _request()
    r1.id = 10
    decision = await classify_with_jev(
        AsyncMock(),
        supplier=_supplier(),
        business_connection_id=None,
        chat_id=1,
        message_id=1,
        message_text="117900",
        candidates=[r1],
        outgoing={10: JevOutboundFact(sent_at=datetime(2026, 10, 30, 9, 0, tzinfo=UTC), kind="bargain")},
    )
    assert decision.reason_code == "jev_timeout"


def test_price_parser_keeps_thousands_suffix() -> None:
    from app.parsers.price_parse import parse_price_from_text

    parsed = parse_price_from_text("17 pro 256 blue 1sim 127.5к")
    assert parsed.price == Decimal("127500")


@pytest.mark.asyncio
async def test_batch_reply_preloads_outgoing_once_and_respects_bargaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from app.db.models import MessageIn, Quote, QuoteSource
    from app.services.candidate_loader_service import JevOutboundFact
    from app.services.supplier_batch_service import process_batch_supplier_reply

    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.setenv("JEV_SHADOW_MODE", "false")
    monkeypatch.setenv("SUPPLIER_BATCH_REPLY_ENABLED", "true")
    get_settings.cache_clear()

    r1 = Request(
        id=11,
        status=RequestStatus.bargaining,
        source_text="iPhone 17 Pro 256 Blue",
        normalized_json={"model": "iPhone 17 Pro", "storage": "256", "color": "Blue"},
        group_chat_id=100,
        employee_id=1,
    )
    r2 = Request(
        id=12,
        status=RequestStatus.awaiting_answers,
        source_text="iPhone 15 128 Black",
        normalized_json={"model": "iPhone 15", "storage": "128", "color": "Black"},
        group_chat_id=100,
        employee_id=1,
    )

    outbound_calls = 0

    async def _mock_load_candidates(*args: object, **kwargs: object) -> list[Request]:
        del args, kwargs
        return [r1, r2]

    async def _mock_outbound(*args: object, **kwargs: object) -> dict[int, JevOutboundFact]:
        nonlocal outbound_calls
        del args, kwargs
        outbound_calls += 1
        return {
            11: JevOutboundFact(sent_at=datetime(2026, 10, 30, 9, 0, tzinfo=UTC), kind="bargain"),
            12: JevOutboundFact(sent_at=datetime(2026, 10, 30, 9, 1, tzinfo=UTC), kind="ask"),
        }

    async def _no_cache(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _no_persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    class _MatchR1ThenR2:
        calls = 0

        async def classify_supplier_message(
            self,
            *,
            message_text: str,
            candidates: list[JevCandidate],
            correlation_id: str,
        ) -> JevProviderResponse:
            del message_text, candidates, correlation_id
            _MatchR1ThenR2.calls += 1
            choice = "request:11" if _MatchR1ThenR2.calls == 1 else "request:12"
            return JevProviderResponse(
                model="jev-1.14.0",
                message_choice="price_offer",
                price_probability=Decimal("0.99"),
                binding_choice=choice,
                binding_probabilities={choice: Decimal("0.99"), "none": Decimal("0.01")},
                binding_confidence=Decimal("0.98"),
            )

    upserted_bargain_flags: list[tuple[int, bool]] = []

    async def _mock_upsert(
        session: object,
        request_id: int,
        supplier_id: int,
        *,
        price: Decimal | None,
        qty: int | None = None,
        available: bool | None = None,
        condition: str | None = None,
        source: QuoteSource = QuoteSource.regex,
        confidence: float | None = None,
        bargain: bool = False,
    ) -> Quote:
        del session, supplier_id, price, qty, available, condition, source, confidence
        upserted_bargain_flags.append((request_id, bargain))
        return Quote(id=1, request_id=request_id, supplier_id=1, source=QuoteSource.regex)

    class _FakeReviewItem:
        bind_status = "pending"
        request_id = None
        bind_method = None
        parsed_json: dict[str, object] = {}

    async def _mock_review_item(*args: object, **kwargs: object) -> _FakeReviewItem:
        del args, kwargs
        return _FakeReviewItem()

    monkeypatch.setattr(
        "app.services.supplier_batch_service.load_eligible_candidates",
        _mock_load_candidates,
    )
    monkeypatch.setattr(
        "app.services.supplier_batch_service.load_jev_outbound_facts",
        _mock_outbound,
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.load_jev_outbound_facts",
        _mock_outbound,
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.lookup_cached_decision",
        _no_cache,
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.persist_jev_decision",
        _no_persist,
    )
    monkeypatch.setattr(
        "app.services.supplier_batch_service.find_clear_text_match",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "app.services.supplier_batch_service.upsert_quote",
        _mock_upsert,
    )
    monkeypatch.setattr(
        "app.services.supplier_batch_service.ensure_review_item_for_message",
        _mock_review_item,
    )
    set_jev_client(_MatchR1ThenR2())

    msg_in = MessageIn(id=55, supplier_id=1, chat_id=10, tg_message_id=777, raw_text="x")
    telegram_mock = AsyncMock()
    status = await process_batch_supplier_reply(
        AsyncMock(),
        supplier=_supplier(),
        raw_text="iPhone 17 Pro 256 Blue 117900\niPhone 15 128 Black 68000",
        message_in=msg_in,
        chat_id=10,
        business_connection_id=None,
        telegram=telegram_mock,
    )
    assert status == "ok"
    assert outbound_calls == 1
    assert upserted_bargain_flags == [(11, True), (12, False)]
