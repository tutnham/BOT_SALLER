"""Request normalization for /ask (TECH DOC §9.1 step 2, §8.1, §8.5)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageOut, Request
from app.llm.client import LLMProviderError, set_llm_client
from app.services.request_service import build_normalized_json


def _ask_update(
    *,
    update_id: int,
    from_id: int,
    chat_id: int,
    text: str,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": from_id, "first_name": "Employee"},
            "chat": {"id": chat_id, "type": "supergroup"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_regex_normalize_without_llm(
    db_session: AsyncSession,
    mock_llm,
) -> None:
    source = "iPhone 15 Pro 256GB чёрный 2 шт"
    normalized = await build_normalized_json(db_session, source)

    assert mock_llm.normalize_calls == 0
    assert "iPhone" in normalized["model"]
    assert normalized["storage"] == "256GB"
    assert normalized["color"] == "чёрный"
    assert normalized["qty"] == 2
    assert normalized["confidence"] >= 0.75


@pytest.mark.asyncio
async def test_llm_normalize_on_ambiguous_text(
    db_session: AsyncSession,
    mock_llm,
) -> None:
    set_llm_client(mock_llm)
    try:
        mock_llm.normalize_result = {
            "model": "Samsung Galaxy S25",
            "storage": "256GB",
            "color": None,
            "region": None,
            "sim": None,
            "qty": None,
            "condition": None,
            "confidence": 0.85,
        }
        source = "нужен телефон самсунг новый"

        normalized = await build_normalized_json(db_session, source)
        assert mock_llm.normalize_calls == 1
        assert normalized["model"] == "Samsung Galaxy S25"
        assert normalized["confidence"] >= 0.75
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_normalize_cache_skips_second_llm_call(
    db_session: AsyncSession,
    mock_llm,
) -> None:
    set_llm_client(mock_llm)
    try:
        mock_llm.normalize_result = {
            "model": "Pixel 9",
            "storage": None,
            "color": None,
            "region": None,
            "sim": None,
            "qty": None,
            "condition": None,
            "confidence": 0.8,
        }
        source = "нужен pixel 9"

        await build_normalized_json(db_session, source)
        await build_normalized_json(db_session, source)
        assert mock_llm.normalize_calls == 1
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_llm_failure_still_creates_request(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_supplier_count: int,
    mock_llm,
    mock_telegram,
) -> None:
    mock_llm.raise_normalize = LLMProviderError("llm_down")
    source = "нужен pixel 9"

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_ask_update(
            update_id=57001,
            from_id=seed_employee_telegram_id,
            chat_id=seed_group_chat_id,
            text=f"/ask {source}",
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    request = await db_session.scalar(select(Request).order_by(Request.id.desc()))
    assert request is not None
    assert request.source_text == source
    assert request.normalized_json is not None
    assert request.normalized_json["model"] == "Pixel 9"
    assert request.normalized_json["confidence"] < 0.75

    outs = await db_session.scalars(select(MessageOut))
    assert len(list(outs)) == seed_supplier_count


@pytest.mark.asyncio
async def test_ask_template_has_request_id_no_competitor_prices(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    source = "iPhone 15 Pro 256GB чёрный 2 шт"
    mock_telegram.sent.clear()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_ask_update(
            update_id=57002,
            from_id=seed_employee_telegram_id,
            chat_id=seed_group_chat_id,
            text=f"/ask {source}",
        ),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    request = await db_session.scalar(select(Request).order_by(Request.id.desc()))
    assert request is not None

    supplier_sends = [
        txt for chat_id, txt, *_ in mock_telegram.sent if chat_id > 0 and chat_id != seed_group_chat_id
    ]
    assert supplier_sends
    assert all(f"Запрос #{request.id}" in txt for txt in supplier_sends)
    assert all("85000" not in txt for txt in supplier_sends)
    assert all("82000" not in txt for txt in supplier_sends)
