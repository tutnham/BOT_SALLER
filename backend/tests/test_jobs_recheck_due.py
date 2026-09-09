"""POST /jobs/recheck-due — auth, due filter, idempotency (TECH DOC §7.3, §9.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MessageKind,
    MessageOut,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
    Supplier,
)
from app.services.request_service import create_request
from app.telegram.client import TelegramSendError


async def _due_request(
    db_session: AsyncSession,
    *,
    seed_employee,
    seed_group_chat_id: int,
    mock_telegram,
    seed_suppliers: list[Supplier],
    due: bool = True,
    with_quote: bool = True,
) -> Request:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=mock_telegram,
    )
    if with_quote:
        db_session.add(
            Quote(
                request_id=request.id,
                supplier_id=seed_suppliers[0].id,
                price_initial=Decimal("85000"),
                source=QuoteSource.manual,
                confidence=1.0,
            )
        )
    request.status = RequestStatus.needs_recheck
    if due:
        request.recheck_at = datetime.now(UTC) - timedelta(minutes=1)
    else:
        request.recheck_at = datetime.now(UTC) + timedelta(hours=2)
    await db_session.flush()
    return request


@pytest.mark.asyncio
async def test_recheck_due_requires_secret(webhook_client: AsyncClient) -> None:
    resp = await webhook_client.post("/jobs/recheck-due", json={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_recheck_due_sends_only_due_and_clears_recheck_at(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    due = await _due_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
        due=True,
    )
    not_due = await _due_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
        due=False,
    )
    mock_telegram.sent.clear()

    resp = await webhook_client.post(
        "/jobs/recheck-due",
        json={},
        headers={"X-Webhook-Secret": "test-webhook-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["processed"] == 1
    assert body["skipped"] == 0

    await db_session.refresh(due)
    await db_session.refresh(not_due)
    assert due.recheck_at is None
    assert due.status == RequestStatus.needs_recheck
    assert not_due.recheck_at is not None

    out = await db_session.scalar(
        select(MessageOut).where(
            MessageOut.request_id == due.id,
            MessageOut.kind == MessageKind.recheck,
        )
    )
    assert out is not None
    assert f"#{due.id}" in out.text
    assert seed_suppliers[0].id == out.supplier_id

    not_due_out = await db_session.scalar(
        select(MessageOut).where(
            MessageOut.request_id == not_due.id,
            MessageOut.kind == MessageKind.recheck,
        )
    )
    assert not_due_out is None


@pytest.mark.asyncio
async def test_recheck_due_safe_rerun_no_duplicate_send(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request = await _due_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
    )
    mock_telegram.sent.clear()

    headers = {"X-Webhook-Secret": "test-webhook-secret"}
    first = await webhook_client.post("/jobs/recheck-due", json={}, headers=headers)
    assert first.status_code == 200
    assert first.json()["processed"] == 1

    second = await webhook_client.post("/jobs/recheck-due", json={}, headers=headers)
    assert second.status_code == 200
    assert second.json()["processed"] == 0

    count = await db_session.scalar(
        select(func.count())
        .select_from(MessageOut)
        .where(
            MessageOut.request_id == request.id,
            MessageOut.kind == MessageKind.recheck,
        )
    )
    assert count == 1


@pytest.mark.asyncio
async def test_recheck_due_skip_without_quote(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    request = await _due_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
        with_quote=False,
    )
    mock_telegram.sent.clear()

    resp = await webhook_client.post(
        "/jobs/recheck-due",
        json={},
        headers={"X-Webhook-Secret": "test-webhook-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["processed"] == 0
    assert body["skipped"] == 1

    await db_session.refresh(request)
    assert request.recheck_at is None

    group_sends = [txt for cid, txt, *_ in mock_telegram.sent if cid == seed_group_chat_id]
    assert any("пропущена" in txt for txt in group_sends)


@pytest.mark.asyncio
async def test_recheck_due_one_failure_does_not_abort_batch(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
    mock_telegram,
) -> None:
    bad = await _due_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
    )
    good = await _due_request(
        db_session,
        seed_employee=seed_employee,
        seed_group_chat_id=seed_group_chat_id,
        mock_telegram=mock_telegram,
        seed_suppliers=seed_suppliers,
    )
    mock_telegram.sent.clear()

    original_send = mock_telegram.send_message

    async def flaky_send(chat_id: int, text: str, *, parse_mode: str | None = None) -> int:
        if chat_id == seed_suppliers[0].telegram_id and f"#{bad.id}" in text:
            raise TelegramSendError("temporary failure")
        return await original_send(chat_id, text, parse_mode=parse_mode)

    mock_telegram.send_message = AsyncMock(side_effect=flaky_send)

    resp = await webhook_client.post(
        "/jobs/recheck-due",
        json={},
        headers={"X-Webhook-Secret": "test-webhook-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["processed"] == 1
    assert body["failed"] == 1

    good_out = await db_session.scalar(
        select(MessageOut).where(
            MessageOut.request_id == good.id,
            MessageOut.kind == MessageKind.recheck,
        )
    )
    assert good_out is not None
