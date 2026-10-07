"""Multi-line RFQ supplier reply parsing (one item per line)."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageIn, QuoteSource, Supplier
from app.parsers.price_parse import parse_price_from_text
from app.parsers.product_normalizer import extract_product_attrs
from app.parsers.regex_parser import parse_supplier_reply
from app.services.candidate_loader_service import load_eligible_candidates
from app.services.quote_service import display_price_for_group, upsert_quote
from app.services.reply_binding_service import find_clear_text_match
from app.services.supplier_review_service import ensure_review_item_for_message
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template

_PRICE_LINE_RE = re.compile(r"\d[\d\s]{2,}|\d+\s*(?:к|k|тыс|руб|₽)", re.IGNORECASE)


def is_multiline_rfq_candidate(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    price_lines = sum(1 for line in lines if _PRICE_LINE_RE.search(line))
    return price_lines >= 2


async def process_batch_supplier_reply(
    session: AsyncSession,
    *,
    supplier: Supplier,
    raw_text: str,
    message_in: MessageIn,
    chat_id: int,
    business_connection_id: str | None,
    telegram: TelegramClientProtocol,
) -> str:
    settings = get_settings()
    if not settings.supplier_batch_reply_enabled:
        return "skip"

    candidates = await load_eligible_candidates(
        session,
        supplier=supplier,
        chat_id=chat_id,
        business_connection_id=business_connection_id,
    )
    if not candidates:
        return "skip"

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    handled = False
    for line_no, line in enumerate(lines, start=1):
        if not _PRICE_LINE_RE.search(line):
            continue
        line_attrs = extract_product_attrs(line)
        match = find_clear_text_match(line, line_attrs, candidates)
        price_result = parse_price_from_text(line)
        if match is not None and price_result.price is not None:
            parsed = parse_supplier_reply(line)
            quote = await upsert_quote(
                session,
                match.id,
                supplier.id,
                price=price_result.price,
                qty=parsed.qty,
                available=parsed.available,
                condition=parsed.condition,
                source=QuoteSource.regex,
                confidence=max(parsed.confidence, price_result.confidence),
            )
            if quote is not None:
                await telegram.send_message(
                    match.group_chat_id,
                    render_template(
                        "supplier_quote_parsed",
                        request_id=match.id,
                        available=parsed.available,
                        price=display_price_for_group(quote),
                        qty=parsed.qty,
                    ),
                )
            item = await ensure_review_item_for_message(
                session,
                message_in=message_in,
                line_no=line_no,
                raw_line=line,
                candidate_request_ids=[match.id],
            )
            item.bind_status = "resolved"
            item.request_id = match.id
            item.bind_method = "product_match"
            handled = True
        else:
            await ensure_review_item_for_message(
                session,
                message_in=message_in,
                line_no=line_no,
                raw_line=line,
                conflict_reason="ambiguous_line",
                candidate_request_ids=[c.id for c in candidates],
            )
            handled = True
    message_in.bind_status = "bound" if handled else message_in.bind_status
    await session.flush()
    return "ok" if handled else "skip"
