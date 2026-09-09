"""Inline keyboard helpers with strict callback_data protocol.

Callback payload format: ``namespace:action:arg:page``

* ``namespace`` — top-level menu area (e.g. ``admin``)
* ``action`` — operation identifier (e.g. ``sup_list``)
* ``arg`` — integer id or ``0`` when unused; string keys must be encoded via
  short aliases (callback_data max 64 bytes)
* ``page`` — non-negative integer for paginated lists

Telegram limits callback_data to 64 bytes, so we keep every part short and
never put free text in it. The parser is strict: any malformed payload is
rejected and ignored.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

_MAX_CB = 64


def _join(parts: list[str]) -> str:
    data = ":".join(parts)
    if len(data.encode("utf-8")) > _MAX_CB:
        raise ValueError(f"callback_data too long: {len(data.encode('utf-8'))} bytes")
    return data


class CallbackData(BaseModel):
    """Typed callback data extracted from Telegram inline button."""

    namespace: str = Field(..., pattern=r"^[a-z0-9_]{1,16}$")
    action: str = Field(..., pattern=r"^[a-z0-9_]{1,24}$")
    arg: int = Field(default=0)
    page: int = Field(default=0)

    @field_validator("namespace", "action")
    @classmethod
    def _no_colons(cls, value: str) -> str:
        if ":" in value:
            raise ValueError("no colons allowed")
        return value

    def encode(self) -> str:
        return _join([self.namespace, self.action, str(self.arg), str(self.page)])

    @classmethod
    def decode(cls, raw: str | None) -> CallbackData | None:
        if raw is None:
            return None
        parts = raw.split(":")
        if len(parts) != 4:
            return None
        try:
            arg = int(parts[2])
            page = int(parts[3])
        except ValueError:
            return None
        try:
            return cls(namespace=parts[0], action=parts[1], arg=arg, page=page)
        except ValidationError:
            return None


def button(text: str, *, cd: CallbackData) -> dict[str, Any]:
    """Single inline keyboard button."""
    return {"text": text, "callback_data": cd.encode()}


def url_button(text: str, url: str) -> dict[str, Any]:
    return {"text": text, "url": url}


def paginated_keyboard(
    items: list[tuple[str, CallbackData]],
    *,
    page: int,
    page_size: int = 8,
    nav_callback: CallbackData,
) -> list[list[dict[str, Any]]]:
    """Build rows of buttons with Prev/Next navigation appended.

    ``items`` is a list of ``(button_text, callback_data)`` tuples. Each button
    gets its own row (one-item rows) to keep long supplier names readable.
    """
    total = len(items)
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]
    rows: list[list[dict[str, Any]]] = [[button(text, cd=cd)] for text, cd in page_items]

    nav_row: list[dict[str, Any]] = []
    if page > 0:
        nav_row.append(
            button(
                "◀ Назад",
                cd=nav_callback.model_copy(update={"page": page - 1}),
            )
        )
    if end < total:
        nav_row.append(
            button(
                "Вперед ▶",
                cd=nav_callback.model_copy(update={"page": page + 1}),
            )
        )
    if nav_row:
        rows.append(nav_row)
    return rows


def inline_keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Wrap rows into Telegram ``reply_markup`` object."""
    return {"inline_keyboard": rows}


def menu_button(text: str, action: str, arg: int = 0, page: int = 0) -> dict[str, Any]:
    return button(text, cd=CallbackData(namespace="admin", action=action, arg=arg, page=page))
