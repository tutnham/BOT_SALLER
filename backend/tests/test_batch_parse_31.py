from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.parsers.batch_lines import split_batch_source_text
from app.services.batch_request_service import create_draft_batch

FIXTURE = Path(__file__).parent / "fixtures" / "bulk_request_31_lines.txt"


def test_fixture_has_31_lines() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    lines = split_batch_source_text(text)
    assert len(lines) == 31
    assert lines[0].line_no == 1
    assert lines[-1].line_no == 31
    assert lines[0].source_text == "14 128GB Yellow"


@pytest.mark.asyncio
async def test_draft_batch_creates_31_requests(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    batch = await create_draft_batch(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text=text,
    )
    await db_session.refresh(batch, attribute_names=["requests"])
    assert batch.items_total == 31
    assert len(batch.requests) == 31
    line_numbers = [r.line_no for r in batch.requests]
    assert line_numbers == list(range(1, 32))
    texts = [r.source_text for r in sorted(batch.requests, key=lambda x: x.line_no or 0)]
    fixture_lines = [
        line.strip()
        for line in text.replace("\r\n", "\n").split("\n")
        if line.strip()
    ]
    assert texts == fixture_lines
    keys = {r.canonical_sku_key for r in batch.requests}
    assert None not in keys
    assert len(keys) == 31
