"""Webhook tests for /recheck (TECH DOC §9.4, Phase 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.db.models import RequestStatus
from app.services.request_service import create_request
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


def _employee_command_update(
    *,
    update_id: int,
    employee_telegram_id: int,
    group_chat_id: int,
    text: str,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": employee_telegram_id, "first_name": "Employee"},
            "chat": {"id": group_chat_id, "type": "supergroup"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_recheck_default_hours_three(
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
    before = datetime.now(UTC)
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=52001,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/recheck {request.id}",
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
    assert request.recheck_at >= before + timedelta(hours=2, minutes=55)
    assert request.recheck_at <= before + timedelta(hours=3, minutes=5)

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("повторная проверка через 3 ч" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_recheck_custom_hours_in_range(
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
    before = datetime.now(UTC)
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=52002,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/recheck {request.id} 5",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    await db_session.refresh(request)
    assert request.status == RequestStatus.needs_recheck
    assert request.recheck_at is not None
    assert request.recheck_at >= before + timedelta(hours=4, minutes=55)
    assert request.recheck_at <= before + timedelta(hours=5, minutes=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [1, 6])
async def test_recheck_invalid_hours_rejected(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_employee_telegram_id: int,
    seed_group_chat_id: int,
    mock_telegram,
    hours: int,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=mock_telegram,
    )
    original_status = request.status
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=52010 + hours,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/recheck {request.id} {hours}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200

    await db_session.refresh(request)
    assert request.status == original_status
    assert request.recheck_at is None

    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("от 2 до 5" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_recheck_closed_request_rejected(
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
    request.status = RequestStatus.closed
    await db_session.flush()
    mock_telegram.sent.clear()

    payload = _employee_command_update(
        update_id=52020,
        employee_telegram_id=seed_employee_telegram_id,
        group_chat_id=seed_group_chat_id,
        text=f"/recheck {request.id}",
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    group_sends = [txt for cid, txt in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("закрыта или отменена" in txt for txt in group_sends)
