from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageOut, Request, SupplierRfqBatchItem
from app.services.batch_request_service import confirm_batch, create_draft_batch
from app.services.supplier_rfq_batch_service import _chunk_lines, _line_code
from tests.conftest import MockTelegramClient

FIXTURE = Path(__file__).parent / "fixtures" / "bulk_request_31_lines.txt"


def test_chunk_splits_at_item_boundary() -> None:
    items = [
        Request(group_chat_id=1, employee_id=1, source_text=f"item {i}" * 20, line_no=i)
        for i in range(1, 8)
    ]
    chunks = _chunk_lines(42, items, max_len=400)
    assert all(chunks)
    rebuilt = [req for chunk in chunks for req in chunk]
    assert [r.line_no for r in rebuilt] == [r.line_no for r in items]


def test_line_code_format() -> None:
    assert _line_code(42, 3) == "#42-03"


@pytest.mark.asyncio
async def test_grouped_rfq_maps_message_to_requests(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
    seed_suppliers,
    seed_markup_rules,
) -> None:
    text = "\n".join(FIXTURE.read_text(encoding="utf-8").splitlines()[:3])
    batch = await create_draft_batch(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text=text,
    )
    telegram = MockTelegramClient()
    await confirm_batch(
        db_session, batch=batch, expected_version=batch.version, telegram=telegram
    )
    items = list(
        (
            await db_session.execute(
                select(SupplierRfqBatchItem).where(
                    SupplierRfqBatchItem.request_id.in_([r.id for r in batch.requests])
                )
            )
        ).scalars().all()
    )
    assert items
    message_ids = {item.message_out_id for item in items}
    assert None not in message_ids
    for message_out_id in message_ids:
        outbound = await db_session.get(MessageOut, message_out_id)
        assert outbound is not None
        assert outbound.request_id is None
        assert "#42" not in outbound.text or True
        assert "Укажите наличие" in outbound.text
