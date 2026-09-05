"""Tests for owner-only request purge commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Deal,
    DealOutcome,
    Employee,
    MessageIn,
    MessageKind,
    MessageOut,
    Owner,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
    Supplier,
)
from app.telegram.keyboards import CallbackData

OWNER_TG_ID = 300300300


def _owner_message(update_id: int, text: str, *, owner_id: int = OWNER_TG_ID) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": owner_id},
            "chat": {"id": owner_id, "type": "private"},
            "text": text,
        },
    }


def _callback_payload(
    owner_id: int,
    callback_id: str,
    data: CallbackData,
    *,
    update_id: int = 200,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": owner_id},
            "message": {
                "message_id": 1,
                "chat": {"id": owner_id, "type": "private"},
            },
            "data": data.encode(),
        },
    }


async def _seed_request_with_children(
    db_session: AsyncSession,
    *,
    employee_id: int,
    supplier_id: int,
    status: RequestStatus = RequestStatus.cancelled,
    created_at: datetime | None = None,
) -> Request:
    request = Request(
        group_chat_id=-1001234567890,
        employee_id=employee_id,
        source_text="iPhone 17 256GB",
        normalized_json={"model": "iPhone 17"},
        status=status,
    )
    if created_at is not None:
        request.created_at = created_at
        request.updated_at = created_at
    db_session.add(request)
    await db_session.flush()

    db_session.add_all(
        [
            Quote(
                request_id=request.id,
                supplier_id=supplier_id,
                price_initial=Decimal("85000"),
                source=QuoteSource.manual,
            ),
            Deal(
                request_id=request.id,
                chosen_supplier_id=supplier_id,
                final_price=Decimal("85000"),
                outcome=DealOutcome.won,
            ),
            MessageOut(
                request_id=request.id,
                supplier_id=supplier_id,
                tg_message_id=9001,
                chat_id=-100500,
                text="ask",
                kind=MessageKind.ask,
            ),
            MessageIn(
                request_id=request.id,
                supplier_id=supplier_id,
                tg_message_id=9002,
                chat_id=-100500,
                raw_text="85000",
            ),
        ]
    )
    await db_session.flush()
    return request


@pytest.mark.asyncio
async def test_owner_purge_request_confirmation_button(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    mock_telegram,
    seed_owner: Owner,
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> None:
    request = await _seed_request_with_children(
        db_session,
        employee_id=seed_employee.id,
        supplier_id=seed_suppliers[0].id,
    )

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_message(501, f"/purge_request {request.id}"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    assert mock_telegram.sent
    _, text, markup = mock_telegram.sent[-1]
    assert f"#{request.id}" in text
    assert markup is not None
    callback_data = markup["inline_keyboard"][0][0]["callback_data"]
    assert callback_data == CallbackData(
        namespace="admin", action="purge_confirm", arg=request.id
    ).encode()


@pytest.mark.asyncio
async def test_owner_purge_confirm_deletes_all_children(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> None:
    request = await _seed_request_with_children(
        db_session,
        employee_id=seed_employee.id,
        supplier_id=seed_suppliers[0].id,
    )
    request_id = request.id

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_purge",
            CallbackData(namespace="admin", action="purge_confirm", arg=request_id),
            update_id=502,
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    assert await db_session.get(Request, request_id) is None
    assert (
        await db_session.execute(
            select(func.count()).select_from(Quote).where(Quote.request_id == request_id)
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count()).select_from(Deal).where(Deal.request_id == request_id)
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(MessageOut)
            .where(MessageOut.request_id == request_id)
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(MessageIn)
            .where(MessageIn.request_id == request_id)
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_owner_purge_confirm_not_found_on_repeat(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    mock_telegram,
    seed_owner: Owner,
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> None:
    request = await _seed_request_with_children(
        db_session,
        employee_id=seed_employee.id,
        supplier_id=seed_suppliers[0].id,
    )
    request_id = request.id
    payload = _callback_payload(
        OWNER_TG_ID,
        "cb_purge1",
        CallbackData(namespace="admin", action="purge_confirm", arg=request_id),
        update_id=503,
    )
    resp1 = await webhook_client.post(
        "/telegram/webhook", json=payload, headers=webhook_headers
    )
    assert resp1.json()["status"] == "ok"

    resp2 = await webhook_client.post(
        "/telegram/webhook",
        json={
            **payload,
            "update_id": 504,
            "callback_query": {**payload["callback_query"], "id": "cb_purge2"},
        },
        headers=webhook_headers,
    )
    assert resp2.json()["status"] == "ok"
    assert mock_telegram.edited
    assert "не найдена" in mock_telegram.edited[-1][2]


@pytest.mark.asyncio
async def test_employee_purge_request_ignored(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_employee: Employee,
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_message(505, "/purge_request 1", owner_id=seed_employee.telegram_id),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_purge_old_dry_run_keeps_rows(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    mock_telegram,
    seed_owner: Owner,
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> None:
    old_date = datetime.now(UTC) - timedelta(days=60)
    request = await _seed_request_with_children(
        db_session,
        employee_id=seed_employee.id,
        supplier_id=seed_suppliers[0].id,
        status=RequestStatus.cancelled,
        created_at=old_date,
    )

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_message(506, "/purge_old 30"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    assert mock_telegram.sent
    assert "Dry-run" in mock_telegram.sent[-1][1]
    assert await db_session.get(Request, request.id) is not None


@pytest.mark.asyncio
async def test_purge_old_confirm_deletes_rows(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    mock_telegram,
    seed_owner: Owner,
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> None:
    old_date = datetime.now(UTC) - timedelta(days=60)
    request = await _seed_request_with_children(
        db_session,
        employee_id=seed_employee.id,
        supplier_id=seed_suppliers[0].id,
        status=RequestStatus.closed,
        created_at=old_date,
    )
    request_id = request.id

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_message(507, "/purge_old 30 --confirm"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    assert "Удалено" in mock_telegram.sent[-1][1]
    assert await db_session.get(Request, request_id) is None


@pytest.mark.asyncio
async def test_purge_old_skips_recent_open_requests(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    mock_telegram,
    seed_owner: Owner,
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> None:
    old_date = datetime.now(UTC) - timedelta(days=60)
    open_request = await _seed_request_with_children(
        db_session,
        employee_id=seed_employee.id,
        supplier_id=seed_suppliers[0].id,
        status=RequestStatus.open,
        created_at=old_date,
    )

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_message(508, "/purge_old 30 --confirm"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    assert "0" in mock_telegram.sent[-1][1]
    assert await db_session.get(Request, open_request.id) is not None
