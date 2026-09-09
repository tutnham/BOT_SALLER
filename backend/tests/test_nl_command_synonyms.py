"""NL synonyms for /bargain and /recheck with Privacy Mode gating."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    MessageKind,
    MessageOut,
    Quote,
    QuoteSource,
    RequestStatus,
    Supplier,
)
from app.parsers.nl_commands import parse_nl_command
from app.services.request_service import create_request


def _group_nl_update(
    *,
    update_id: int,
    from_id: int,
    chat_id: int,
    text: str,
    reply_to_bot: bool = False,
    reply_text: str | None = None,
) -> dict:
    message: dict = {
        "message_id": update_id * 10,
        "from": {"id": from_id, "first_name": "Employee"},
        "chat": {"id": chat_id, "type": "supergroup"},
        "text": text,
    }
    if reply_to_bot or reply_text is not None:
        message["reply_to_message"] = {
            "message_id": update_id * 10 - 1,
            "from": {"id": 1, "is_bot": reply_to_bot, "first_name": "Bot"},
            "text": reply_text or "Запрос #1",
        }
    return {"update_id": update_id, "message": message}


def test_parse_nl_recheck_and_bargain() -> None:
    recheck = parse_nl_command("актуализируй цену по #12")
    assert recheck is not None
    assert recheck.intent == "recheck"
    assert recheck.request_id == 12
    assert recheck.hours is None

    bargain = parse_nl_command("пройдём ли по цене ниже 82000 по #5")
    assert bargain is not None
    assert bargain.intent == "bargain"
    assert bargain.request_id == 5
    assert bargain.target_price == 82000.0

    none = parse_nl_command("просто болтовня без команды")
    assert none is None


@pytest.mark.asyncio
async def test_nl_plain_text_without_gate_ignored(
    webhook_client: AsyncClient,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    mock_telegram.sent.clear()
    payload = _group_nl_update(
        update_id=54001,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
        text="актуализируй цену по #1",
        reply_to_bot=False,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert mock_telegram.sent == []


@pytest.mark.asyncio
async def test_nl_recheck_via_reply_to_bot(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=mock_telegram,
    )
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _group_nl_update(
        update_id=54002,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
        text=f"актуализируй цену по #{request.id}",
        reply_to_bot=True,
        reply_text=f"Запрос #{request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    await db_session.refresh(request)
    assert request.status == RequestStatus.needs_recheck
    assert request.recheck_at is not None


@pytest.mark.asyncio
async def test_nl_bargain_needs_price(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=mock_telegram,
    )
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=seed_suppliers[0].id,
            price_initial=Decimal("85000"),
            source=QuoteSource.manual,
            confidence=1.0,
        )
    )
    request.status = RequestStatus.priced
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _group_nl_update(
        update_id=54003,
        from_id=seed_employee_telegram_id,
        chat_id=seed_group_chat_id,
        text=f"можно дешевле по #{request.id}",
        reply_to_bot=True,
        reply_text=f"Запрос #{request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("Укажите целевую цену" in txt for txt in group_sends)

    outs = await db_session.scalars(
        select(MessageOut).where(MessageOut.kind == MessageKind.bargain)
    )
    assert list(outs) == []


@pytest.mark.asyncio
async def test_nl_bargain_via_mention(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    get_settings.cache_clear()
    import os

    os.environ["TELEGRAM_BOT_USERNAME"] = "zakupki_bot"
    get_settings.cache_clear()
    try:
        request, _ = await create_request(
            db_session,
            group_chat_id=seed_group_chat_id,
            employee_id=seed_employee.id,
            source_text="iPhone 17",
            telegram=mock_telegram,
        )
        db_session.add(
            Quote(
                request_id=request.id,
                supplier_id=seed_suppliers[0].id,
                price_initial=Decimal("85000"),
                source=QuoteSource.manual,
                confidence=1.0,
            )
        )
        request.status = RequestStatus.priced
        await db_session.flush()
        mock_telegram.sent.clear()

        payload = {
            "update_id": 54004,
            "message": {
                "message_id": 540040,
                "from": {"id": seed_employee_telegram_id, "first_name": "Employee"},
                "chat": {"id": seed_group_chat_id, "type": "supergroup"},
                "text": f"@zakupki_bot пройдём ли по цене ниже 80000 по #{request.id}",
                "entities": [
                    {"type": "mention", "offset": 0, "length": 12},
                ],
            },
        }
        resp = await webhook_client.post(
            "/telegram/webhook",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        await db_session.refresh(request)
        assert request.status == RequestStatus.bargaining

        bargain_out = await db_session.scalar(
            select(MessageOut).where(
                MessageOut.request_id == request.id,
                MessageOut.kind == MessageKind.bargain,
            )
        )
        assert bargain_out is not None
        assert "80000" in bargain_out.text
    finally:
        os.environ.pop("TELEGRAM_BOT_USERNAME", None)
        get_settings.cache_clear()
