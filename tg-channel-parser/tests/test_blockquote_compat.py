"""Новый constructor цитаты не обрывает разбор и оставляет offset/length."""

from __future__ import annotations

from io import BytesIO

from pyrogram import enums
from pyrogram.raw.core import TLObject
from pyrogram.types import MessageEntity

from app.mtproto.blockquote_compat import install_blockquote_compat


def test_collapsed_blockquote_reads_as_blockquote_entity() -> None:
    install_blockquote_compat()
    payload = BytesIO()
    payload.write((0xF1CCAAAC).to_bytes(4, "little"))
    payload.write((1).to_bytes(4, "little", signed=True))
    payload.write((4).to_bytes(4, "little", signed=True))
    payload.write((12).to_bytes(4, "little", signed=True))
    payload.seek(0)

    entity = TLObject.read(payload)
    parsed = MessageEntity._parse(None, entity, {})

    assert parsed is not None
    assert parsed.type == enums.MessageEntityType.BLOCKQUOTE
    assert parsed.offset == 4
    assert parsed.length == 12
