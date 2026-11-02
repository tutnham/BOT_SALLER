"""Product-aware binding (plan §11.7–10)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageIn, Owner, Quote
from app.handlers.supplier_messages import handle_reply
from app.parsers.product_normalizer import extract_product_attrs
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient, load_supplier_outbound

pytestmark = pytest.mark.usefixtures("supplier_single_candidate_auto_bind")


def test_brandless_18_pro_max_extracts_number_variant() -> None:
    attrs = extract_product_attrs("18 Pro Max")
    assert attrs.number == "18"
    assert attrs.variant == "pro max"


def test_17_pro_message_matches_no_18_pro_request() -> None:
    from app.db.models import Request, RequestStatus
    from app.services.reply_binding_service import compatible_model_candidates

    message = extract_product_attrs("17 Pro 512GB Silver 🇯🇵   107")
    order = extract_product_attrs("18 Pro 512GB Silver")
    request = Request(
        id=6,
        status=RequestStatus.awaiting_answers,
        source_text="18 Pro 512GB Silver",
        normalized_json=order.as_normalized_json(0.9),
        group_chat_id=1,
        employee_id=1,
    )
    assert message.number == "17"
    assert order.number == "18"
    assert compatible_model_candidates(message, [request]) is None


def _open_request(request_id: int, source_text: str):
    from app.db.models import Request, RequestStatus

    order = extract_product_attrs(source_text)
    return Request(
        id=request_id,
        status=RequestStatus.awaiting_answers,
        source_text=source_text,
        normalized_json=order.as_normalized_json(0.9),
        group_chat_id=1,
        employee_id=1,
    )


def test_other_product_lines_do_not_match_a_different_model() -> None:
    from app.services.reply_binding_service import compatible_model_candidates

    cases = (
        ("16 256GB 85000", "iPhone 17 256"),
        ("16 256GB 85000", "iPhone 16 Pro 256"),
        ("iPad Pro 11 256 70000", "iPhone 17 Pro 256"),
        ("Galaxy S24 256 55000", "iPhone 17 Pro 256"),
        ("MacBook Air 13 90000", "MacBook Pro 14 90000"),
    )
    for message_text, request_text in cases:
        kept = compatible_model_candidates(
            extract_product_attrs(message_text),
            [_open_request(1, request_text)],
        )
        assert kept is None, message_text


def test_short_color_price_is_not_a_generation() -> None:
    from app.services.reply_binding_service import compatible_model_candidates

    attrs = extract_product_attrs("silver 85")
    assert attrs.number is None
    assert attrs.variant is None
    kept = compatible_model_candidates(attrs, [_open_request(1, "iPhone 17 Pro 256")])
    assert kept is not None
    assert [item.id for item in kept] == [1]


def test_brandless_storage_line_is_iphone_not_galaxy() -> None:
    from app.services.reply_binding_service import compatible_model_candidates

    attrs = extract_product_attrs("16 256GB 85000")
    assert attrs.number == "16"
    assert attrs.family == "iphone"
    assert attrs.variant == "base"
    assert compatible_model_candidates(attrs, [_open_request(1, "Galaxy S16 256")]) is None
    kept = compatible_model_candidates(attrs, [_open_request(2, "iPhone 16 256")])
    assert kept is not None
    assert [item.id for item in kept] == [2]


def test_same_model_color_mismatch_stays_in_candidate_list() -> None:
    from app.db.models import Request, RequestStatus
    from app.services.reply_binding_service import compatible_model_candidates

    message = extract_product_attrs("17 Pro 512GB Black 107")
    order = extract_product_attrs("17 Pro 512GB Silver")
    request = Request(
        id=6,
        status=RequestStatus.awaiting_answers,
        source_text="17 Pro 512GB Silver",
        normalized_json=order.as_normalized_json(0.9),
        group_chat_id=1,
        employee_id=1,
    )
    kept = compatible_model_candidates(message, [request])
    assert kept is not None
    assert [item.id for item in kept] == [6]


@pytest.mark.asyncio
async def test_one_request_bare_127500_auto_binds(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 1TB Silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        {
            "message_id": 9001,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "127500",
        },
        telegram=mock_telegram,
    )
    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == 127500


@pytest.mark.asyncio
async def test_iphone_18_on_iphone_17_request_no_auto_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    owner = Owner(telegram_id=300300501, name="Review Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 1TB Silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        {
            "message_id": 9002,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "18 Pro Max 256GB Glacier Esim 127500",
        },
        telegram=mock_telegram,
    )
    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert quotes == []
    assert not any(item[0] == owner.telegram_id for item in mock_telegram.sent)
    message_in = await db_session.scalar(select(MessageIn).where(MessageIn.tg_message_id == 9002))
    assert message_in is not None
    assert message_in.bind_status == "ignored"


@pytest.mark.asyncio
async def test_brandless_17_pro_max_auto_binds_matching_request(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256GB",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        {
            "message_id": 9003,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "17 Pro Max 256GB 127500",
        },
        telegram=mock_telegram,
    )
    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None


def _supplier_reply_message(
    supplier_telegram_id: int,
    message_id: int,
    text: str,
    *,
    reply_to_message_id: int,
) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
        "reply_to_message": {"message_id": reply_to_message_id, "text": ""},
    }


@pytest.mark.asyncio
async def test_reply_with_wrong_model_does_not_bind_to_request(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    owner = Owner(telegram_id=300300501, name="Review Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 18 Pro Max 256GB",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    outbound = await load_supplier_outbound(
        db_session, request_id=request.id, supplier_id=supplier.id
    )
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        _supplier_reply_message(
            supplier.telegram_id,
            9010,
            "17 Pro Max 256GB 127500",
            reply_to_message_id=outbound.tg_message_id,
        ),
        telegram=mock_telegram,
    )

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is None
    group_sends = [
        item for item in mock_telegram.sent if item[0] == seed_client_group.chat_id
    ]
    assert group_sends == []
    assert not any(item[0] == owner.telegram_id for item in mock_telegram.sent)
    message_in = await db_session.scalar(select(MessageIn).where(MessageIn.tg_message_id == 9010))
    assert message_in is not None
    assert message_in.bind_status == "ignored"


@pytest.mark.asyncio
async def test_reply_with_matching_model_still_binds(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256GB",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    outbound = await load_supplier_outbound(
        db_session, request_id=request.id, supplier_id=supplier.id
    )
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        _supplier_reply_message(
            supplier.telegram_id,
            9011,
            "Есть, 85000 руб",
            reply_to_message_id=outbound.tg_message_id,
        ),
        telegram=mock_telegram,
    )

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == 85000


@pytest.mark.asyncio
async def test_17_pro_price_against_18_pro_request_is_ignored(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    """Supplier price for 17 Pro must not be offered against an open 18 Pro request."""
    owner = Owner(telegram_id=300300502, name="Review Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="18 Pro 512GB Silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        {
            "message_id": 9004,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "17 Pro 512GB Silver 🇯🇵   107",
        },
        telegram=mock_telegram,
    )

    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert quotes == []
    assert not any(item[0] == owner.telegram_id for item in mock_telegram.sent)
    message_in = await db_session.scalar(select(MessageIn).where(MessageIn.tg_message_id == 9004))
    assert message_in is not None
    assert message_in.bind_status == "ignored"


@pytest.mark.asyncio
async def test_same_model_different_color_still_asks_operator(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    owner = Owner(telegram_id=300300503, name="Review Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="17 Pro 512GB Silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        {
            "message_id": 9005,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "17 Pro 512GB Black 107",
        },
        telegram=mock_telegram,
    )

    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert quotes == []
    assert any(item[0] == owner.telegram_id for item in mock_telegram.sent)
