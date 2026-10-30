"""Jev stays off the production path unless explicitly enabled outside shadow mode."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Quote, SupplierAiDecision
from app.handlers.supplier_messages import handle_reply
from app.integrations.jev.client import set_jev_client
from app.integrations.jev.exceptions import JevTimeout
from app.integrations.jev.models import JevCandidate, JevProviderResponse
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient


class _Boom:
    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del message_text, candidates, correlation_id
        raise AssertionError("jev must not be called")


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


class _AcceptFirst:
    seen: list[JevCandidate] = []

    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del message_text, correlation_id
        _AcceptFirst.seen = list(candidates)
        choice = candidates[0].candidate_id
        return JevProviderResponse(
            model="jev-1.14.0",
            message_choice="price_offer",
            price_probability=Decimal("0.99"),
            binding_choice=choice,
            binding_probabilities={
                choice: Decimal("0.99"),
                "none": Decimal("0.005"),
                "ambiguous": Decimal("0.005"),
            },
            binding_confidence=Decimal("0.98"),
        )


def _enable(monkeypatch: pytest.MonkeyPatch, *, shadow: bool) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "test-jev-key")
    monkeypatch.setenv("JEV_SHADOW_MODE", "true" if shadow else "false")
    monkeypatch.setenv("JEV_FALLBACK_TO_EXISTING_LLM", "false")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    yield
    set_jev_client(None)
    get_settings.cache_clear()


def _message(supplier_telegram_id: int, message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
    }


@pytest.mark.asyncio
async def test_disabled_does_not_call_jev(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()
    set_jev_client(_Boom())
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    status = await handle_reply(
        db_session,
        _message(supplier.telegram_id, 8801, "117900"),
        telegram=mock_telegram,
    )
    assert status == "ok"


@pytest.mark.asyncio
async def test_shadow_mode_does_not_create_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, shadow=True)
    set_jev_client(_AcceptFirst())
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        _message(supplier.telegram_id, 8802, "117900"),
        telegram=mock_telegram,
    )
    quotes = (await db_session.execute(select(func.count()).select_from(Quote))).scalar_one()
    assert quotes == 0
    stored = (await db_session.execute(select(func.count()).select_from(SupplierAiDecision))).scalar_one()
    assert stored == 1
    assert _AcceptFirst.seen
    sent = _AcceptFirst.seen[0]
    assert sent.order_text == "iPhone 17 Pro 256 Blue"
    assert "iPhone 17 Pro 256 Blue" in sent.description
    assert sent.outgoing_at
    assert sent.rfq_kind == "ask"


@pytest.mark.asyncio
async def test_active_mode_binds_clear_price(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, shadow=False)
    set_jev_client(_AcceptFirst())
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        _message(supplier.telegram_id, 8803, "117900"),
        telegram=mock_telegram,
    )
    quote = await db_session.scalar(select(Quote).where(Quote.request_id == request.id))
    assert quote is not None
    assert quote.price_initial == Decimal("117900")


@pytest.mark.asyncio
async def test_timeout_falls_back_without_webhook_error(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, shadow=False)
    set_jev_client(_Timeout())
    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    status = await handle_reply(
        db_session,
        _message(supplier.telegram_id, 8804, "117900"),
        telegram=mock_telegram,
    )
    assert status == "ok"
    quotes = (await db_session.execute(select(func.count()).select_from(Quote))).scalar_one()
    assert quotes == 0


@pytest.mark.asyncio
async def test_explicit_request_id_bypasses_jev(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, shadow=False)
    set_jev_client(_Boom())
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    status = await handle_reply(
        db_session,
        _message(supplier.telegram_id, 8805, f"заявка #{request.id} 117900"),
        telegram=mock_telegram,
    )
    assert status == "ok"


@pytest.mark.asyncio
async def test_reply_binding_bypasses_jev(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, shadow=False)
    set_jev_client(_Boom())
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    from tests.conftest import load_supplier_outbound

    outbound = await load_supplier_outbound(
        db_session, request_id=request.id, supplier_id=supplier.id
    )
    payload = _message(supplier.telegram_id, 8806, "117900")
    payload["reply_to_message"] = {"message_id": outbound.tg_message_id}
    status = await handle_reply(db_session, payload, telegram=mock_telegram)
    assert status == "ok"
