from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.db.models import (
    Deal,
    Employee,
    MessageIn,
    MessageKind,
    MessageOut,
    Quote,
    QuoteSource,
    ReportCache,
    Request,
    RequestStatus,
    Supplier,
)
from app.llm.client import LLMProviderError, set_llm_client
from app.services.report_service import build_report
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_report_service_metrics_and_cache_reuse(
    db_session: AsyncSession,
    mock_llm,
) -> None:
    set_llm_client(mock_llm)
    try:
        employee = Employee(telegram_id=10101, name="Emp", active=True)
        supplier = Supplier(telegram_id=20202, name="Supp", active=True, dm_ok=True)
        db_session.add_all([employee, supplier])
        await db_session.flush()

        req = Request(
            group_chat_id=-100,
            employee_id=employee.id,
            source_text="Need phones",
            normalized_json={"model": "iPhone"},
            status=RequestStatus.closed,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        await db_session.flush()

        db_session.add_all(
            [
                MessageOut(
                    request_id=req.id,
                    supplier_id=supplier.id,
                    tg_message_id=1,
                    chat_id=supplier.telegram_id or 0,
                    text="ask",
                    kind=MessageKind.ask,
                ),
                MessageIn(
                    request_id=req.id,
                    supplier_id=supplier.id,
                    tg_message_id=2,
                    chat_id=supplier.telegram_id or 0,
                    raw_text="85000",
                ),
                Quote(
                    request_id=req.id,
                    supplier_id=supplier.id,
                    price_initial=100000,
                    qty=1,
                    available=True,
                    source=QuoteSource.manual,
                    confidence=1.0,
                ),
                Deal(
                    request_id=req.id,
                    chosen_supplier_id=supplier.id,
                    final_price=95000,
                ),
            ]
        )
        await db_session.flush()

        mock_llm.report_calls = 0
        mock_llm.report_result = "<b>HTML отчет</b>"

        text1 = await build_report(db_session, period="day")
        assert "<b>HTML отчет</b>" == text1
        assert mock_llm.report_calls == 1

        cache = await db_session.scalar(select(ReportCache))
        assert cache is not None
        metrics = cache.metrics_json
        assert metrics["requests_total"] == 1
        assert metrics["deals_closed"] == 1
        assert metrics["messages_out_total"] == 1
        assert metrics["messages_in_total"] == 1
        assert metrics["avg_price_initial"] == 100000.0
        assert metrics["avg_final_price"] == 95000.0
        assert metrics["savings_vs_initial"] == 5000.0
        assert metrics["open_requests"] == 0
        assert metrics["top_suppliers"][0]["name"] == "Supp"

        text2 = await build_report(db_session, period="day")
        assert text2 == text1
        assert mock_llm.report_calls == 1
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_report_service_uses_fallback_when_llm_unavailable(
    db_session: AsyncSession,
    mock_llm,
) -> None:
    set_llm_client(mock_llm)
    try:
        mock_llm.raise_report = LLMProviderError("provider_down")
        text = await build_report(db_session, period="day")
        assert "<b>Отчёт закупок</b>" in text
        assert "05." in text or "За период активности не зафиксировано." in text
        assert "н/д" not in text
    finally:
        set_llm_client(None)
