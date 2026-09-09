"""Handle Telegram chat membership events (bot added/removed from groups)."""

from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClientGroup, SupplierChat
from app.services import admin_service
from app.telegram.client import TelegramClientProtocol, get_telegram_client
from app.telegram.keyboards import inline_keyboard, menu_button
from app.templates.messages_ru import render_template
from app.utils.whitelist import list_owner_telegram_ids


async def handle_my_chat_member(
    session: AsyncSession,
    my_chat_member: dict,
) -> str:
    """Process bot being added to or removed from a group/supergroup."""
    chat = my_chat_member.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    title = chat.get("title")
    if chat_id is None or chat_type not in {"group", "supergroup"}:
        return "ignored"

    old_member = (my_chat_member.get("old_chat_member") or {}).get("status")
    new_member = (my_chat_member.get("new_chat_member") or {}).get("status")
    is_added = old_member in {"left", "kicked"} and new_member == "member"
    is_removed = old_member == "member" and new_member in {"left", "kicked"}

    if not is_added and not is_removed:
        return "ignored"

    telegram = get_telegram_client()

    if is_added:
        from_user = my_chat_member.get("from") or {}
        invited_by = from_user.get("id")
        await admin_service.upsert_pending_chat(
            session,
            chat_id=chat_id,
            chat_type=chat_type,
            title=title,
            invited_by_tg_id=invited_by,
        )
        await _notify_owners_about_pending(
            session,
            telegram,
            chat_id=chat_id,
            title=title,
        )
        return "ok"

    # Bot removed: deactivate any bindings to avoid routing failures.
    deactivated = False
    chat_row = (
        await session.execute(
            select(SupplierChat).where(SupplierChat.chat_id == int(chat_id))
        )
    ).scalar_one_or_none()
    if chat_row is not None:
        chat_row.active = False
        chat_row.is_default = False
        deactivated = True
    group_row = (
        await session.execute(
            select(ClientGroup).where(ClientGroup.chat_id == int(chat_id))
        )
    ).scalar_one_or_none()
    if group_row is not None:
        group_row.active = False
        deactivated = True

    if deactivated:
        await _notify_owners_about_removal(
            session,
            telegram,
            chat_id=chat_id,
            title=title,
        )
        await session.flush()
    return "ok"


async def _notify_owners_about_pending(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    chat_id: int,
    title: str | None,
) -> None:
    owners = await list_owner_telegram_ids(session)
    if not owners:
        logger.warning("No owners to notify about pending chat_id={}", chat_id)
        return

    text = render_template(
        "admin_chat_classify_prompt",
        title=title or chat_id,
        chat_id=chat_id,
    )
    rows = [
        [
            menu_button("Поставщик", "pending_as_supplier", chat_id),
            menu_button("Клиенты", "pending_as_client", chat_id),
            menu_button("Игнор", "pending_ignore", chat_id),
        ],
        [menu_button("В меню", "main_menu")],
    ]
    for owner_id in owners:
        try:
            await telegram.send_message(
                owner_id,
                text,
                reply_markup=inline_keyboard(rows),
            )
        except Exception as exc:
            logger.warning("Failed to notify owner {} about pending chat: {}", owner_id, exc)


async def _notify_owners_about_removal(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    chat_id: int,
    title: str | None,
) -> None:
    owners = await list_owner_telegram_ids(session)
    text = render_template(
        "admin_chat_bot_removed",
        title=title or chat_id,
        chat_id=chat_id,
    )
    for owner_id in owners:
        try:
            await telegram.send_message(owner_id, text)
        except Exception as exc:
            logger.warning("Failed to notify owner {} about removal: {}", owner_id, exc)
