"""Pyrogram 2.0.106 знает только старую цитату ``messageEntityBlockquote#20df5d0``.

Актуальная схема — ``#f1ccaaac`` с флагом ``collapsed``. Неизвестный constructor
рвёт разбор всего сообщения, и пост сохраняется без текста либо не сохраняется.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from pyrogram import enums
from pyrogram.raw.all import objects
from pyrogram.raw.core import TLObject
from pyrogram.raw.core.primitives import Int
from pyrogram.types import MessageEntity

# messageEntityBlockquote#f1ccaaac flags:# collapsed:flags.0?true offset:int length:int
_BLOCKQUOTE_ID = 0xF1CCAAAC
_installed = False
_original_parse = MessageEntity._parse


class MessageEntityBlockquoteCollapsed(TLObject):
    """Новая цитата: флаг collapsed + offset/length. Текст остаётся в message."""

    ID = _BLOCKQUOTE_ID
    QUALNAME = "types.MessageEntityBlockquote"
    __slots__ = ["offset", "length", "collapsed"]

    def __init__(self, *, offset: int, length: int, collapsed: bool = False) -> None:
        self.offset = offset
        self.length = length
        self.collapsed = collapsed

    @staticmethod
    def read(b: BytesIO, *args: Any) -> MessageEntityBlockquoteCollapsed:
        flags = Int.read(b)
        offset = Int.read(b)
        length = Int.read(b)
        return MessageEntityBlockquoteCollapsed(
            offset=offset,
            length=length,
            collapsed=bool(flags & 1),
        )


def install_blockquote_compat() -> None:
    """Зарегистрировать constructor и не ронять разбор сущности цитаты."""
    global _installed
    if _installed:
        return
    objects[_BLOCKQUOTE_ID] = MessageEntityBlockquoteCollapsed

    def _parse(client: Any, entity: Any, users: dict) -> MessageEntity | None:
        if isinstance(entity, MessageEntityBlockquoteCollapsed):
            return MessageEntity(
                client=client,
                type=enums.MessageEntityType.BLOCKQUOTE,
                offset=entity.offset,
                length=entity.length,
            )
        try:
            return _original_parse(client, entity, users)
        except ValueError:
            return None

    MessageEntity._parse = staticmethod(_parse)  # type: ignore[method-assign]
    _installed = True


install_blockquote_compat()
