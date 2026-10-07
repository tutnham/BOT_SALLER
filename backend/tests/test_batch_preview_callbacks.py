import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RequestBatchStatus
from app.handlers.batch_callbacks import handle_batch_callback, send_batch_preview
from app.services.batch_request_service import (
    BatchNotEditableError,
    cancel_batch,
    create_draft_batch,
)
from tests.conftest import MockTelegramClient


@pytest.mark.asyncio
async def test_preview_unauthorized_employee(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
) -> None:
    batch = await create_draft_batch(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="14 128GB Yellow\n14 256GB Yellow",
    )
    telegram = MockTelegramClient()
    result = await handle_batch_callback(
        db_session,
        {
            "id": "cb1",
            "from": {"id": 999999999},
            "message": {"chat": {"id": seed_client_group.chat_id, "type": "supergroup"}},
            "data": f"batch:confirm:{batch.id}:{batch.version}",
        },
        telegram=telegram,
    )
    assert result == "ignored"


@pytest.mark.asyncio
async def test_cancel_rejects_after_closed(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
) -> None:
    batch = await create_draft_batch(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="14 128GB Yellow\n14 256GB Yellow",
    )
    await cancel_batch(db_session, batch=batch, expected_version=batch.version)
    with pytest.raises(BatchNotEditableError):
        await cancel_batch(db_session, batch=batch, expected_version=batch.version)
    assert batch.status == RequestBatchStatus.cancelled.value


@pytest.mark.asyncio
async def test_send_preview(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
) -> None:
    batch = await create_draft_batch(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="14 128GB Yellow\n14 256GB Yellow",
    )
    telegram = MockTelegramClient()
    await send_batch_preview(
        db_session,
        batch_id=batch.id,
        chat_id=seed_client_group.chat_id,
        telegram=telegram,
    )
    assert telegram.sent
    assert "Заявка-пакет" in telegram.sent[0][1]
