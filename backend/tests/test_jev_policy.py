"""Application policy for Jev. Provider confidence never bypasses these checks."""

from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.db.models import Request, RequestStatus
from app.integrations.jev.models import (
    JevCandidate,
    JevDecisionAction,
    JevMessageClass,
    JevProviderResponse,
)
from app.integrations.jev.prompts import build_state
from app.services.jev_supplier_classifier import apply_provider_decision


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="tok",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        webhook_secret="sec",
        telegram_webhook_secret_token="sec-telegram",
        metrics_token="metrics",
        jev_price_threshold=Decimal("0.95"),
        jev_bind_threshold=Decimal("0.95"),
        jev_bind_margin=Decimal("0.20"),
    )


def _request(request_id: int, **normalized: object) -> Request:
    return Request(
        id=request_id,
        status=RequestStatus.awaiting_answers,
        source_text="iPhone",
        normalized_json={
            "model": "iPhone 17 Pro",
            "storage": "256",
            "color": "Blue",
            "sim": "1 SIM",
            **normalized,
        },
        group_chat_id=1,
        employee_id=1,
    )


def _response(
    *,
    message_choice: str = "price_offer",
    price: str = "0.99",
    binding: str = "request:1",
    probabilities: dict[str, str] | None = None,
    confidence: str = "0.97",
) -> JevProviderResponse:
    probs = probabilities or {binding: "0.98", "none": "0.01", "ambiguous": "0.01"}
    return JevProviderResponse(
        model="jev-1.14.0",
        message_choice=message_choice,
        price_probability=Decimal(price),
        binding_choice=binding,
        binding_probabilities={key: Decimal(value) for key, value in probs.items()},
        binding_confidence=Decimal(confidence),
    )


def test_price_offer_and_clear_candidate() -> None:
    decision = apply_provider_decision(
        response=_response(),
        candidates=[_request(1)],
        message_text="iPhone 17 Pro 256 Blue 117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.action is JevDecisionAction.AUTO_BIND
    assert decision.reason_code == "jev_price_and_binding_accepted"
    assert decision.selected_candidate_id == 1


def test_not_price_high_confidence_is_ignore() -> None:
    decision = apply_provider_decision(
        response=_response(
            message_choice="not_price",
            price="0.01",
            binding="none",
            probabilities={"none": "0.99", "ambiguous": "0.01"},
        ),
        candidates=[_request(1)],
        message_text="добрый день",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.action is JevDecisionAction.IGNORE
    assert decision.reason_code == "jev_not_price"
    assert decision.message_class is JevMessageClass.NOT_PRICE


def test_ambiguous_message() -> None:
    decision = apply_provider_decision(
        response=_response(message_choice="ambiguous", price="0.50", binding="ambiguous"),
        candidates=[_request(1), _request(2)],
        message_text="может 100",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.action is JevDecisionAction.REVIEW
    assert decision.reason_code == "jev_ambiguous"


def test_low_price_probability() -> None:
    decision = apply_provider_decision(
        response=_response(price="0.80"),
        candidates=[_request(1)],
        message_text="iPhone 17 Pro 256 Blue 117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code == "jev_low_price_probability"
    assert decision.action is JevDecisionAction.REVIEW


def test_low_binding_probability() -> None:
    decision = apply_provider_decision(
        response=_response(probabilities={"request:1": "0.60", "none": "0.40"}),
        candidates=[_request(1)],
        message_text="iPhone 17 Pro 256 Blue 117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code == "jev_low_binding_probability"


def test_low_margin() -> None:
    decision = apply_provider_decision(
        response=_response(
            binding="request:1",
            probabilities={"request:1": "0.96", "request:2": "0.90"},
        ),
        candidates=[_request(1), _request(2, color="Black")],
        message_text="117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code in {"jev_low_margin", "multiple_candidates_bare_price"}
    assert decision.action is JevDecisionAction.REVIEW


def test_product_conflict_blocks_high_confidence() -> None:
    decision = apply_provider_decision(
        response=_response(),
        candidates=[_request(1)],
        message_text="iPhone 16 Pro 256 Blue 117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code == "product_conflict"
    assert decision.action is JevDecisionAction.REVIEW


def test_unknown_candidate_in_policy() -> None:
    decision = apply_provider_decision(
        response=_response(binding="request:99", probabilities={"request:99": "0.99", "none": "0.01"}),
        candidates=[_request(1)],
        message_text="iPhone 17 Pro 256 Blue 117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code == "jev_unknown_candidate"
    assert decision.selected_candidate_id is None


def test_closed_request() -> None:
    closed = _request(1)
    closed.status = RequestStatus.closed
    decision = apply_provider_decision(
        response=_response(),
        candidates=[closed],
        message_text="iPhone 17 Pro 256 Blue 117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code == "request_not_bindable"


def test_inactive_supplier() -> None:
    decision = apply_provider_decision(
        response=_response(),
        candidates=[_request(1)],
        message_text="iPhone 17 Pro 256 Blue 117900",
        supplier_active=False,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code == "supplier_inactive"


def test_route_mismatch() -> None:
    decision = apply_provider_decision(
        response=_response(),
        candidates=[_request(1)],
        message_text="iPhone 17 Pro 256 Blue 117900",
        supplier_active=True,
        route_ok=False,
        settings=_settings(),
    )
    assert decision.reason_code == "route_mismatch"


def test_bare_price_one_candidate() -> None:
    decision = apply_provider_decision(
        response=_response(),
        candidates=[_request(1)],
        message_text="117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.action is JevDecisionAction.AUTO_BIND


def test_bare_price_multiple_candidates() -> None:
    decision = apply_provider_decision(
        response=_response(probabilities={"request:1": "1.0", "request:2": "0"}),
        candidates=[_request(1), _request(2)],
        message_text="117900",
        supplier_active=True,
        route_ok=True,
        settings=_settings(),
    )
    assert decision.reason_code == "multiple_candidates_bare_price"
    assert decision.action is JevDecisionAction.REVIEW


def test_provider_state_has_no_prices() -> None:
    state = build_state(
        "117900",
        [JevCandidate(candidate_id="request:1", description="iPhone 17 Pro 256")],
    )
    blob = str(state)
    assert "markup" not in blob
    assert "price_final" not in blob
    assert "competitor" not in blob
