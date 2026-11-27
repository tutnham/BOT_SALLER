"""parsed_items.sku_key: хвост ключа для всех брендов (минимум не только для iPhone).

Строгий ключ считается здесь же, пачками по id. Отображаемые model/storage не
меняются, поэтому downgrade возвращает ключ strict=False по тем же полям.

Revision ID: 0032_parsed_sku_key_slugs
Revises: 0031_parsed_sku_key_canonical
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_parsed_sku_key_slugs"
down_revision: str | None = "0031_parsed_sku_key_canonical"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 500
_FIELDS = ("model", "storage", "color", "region", "sim")


def _sku_key(row: sa.RowMapping, *, strict: bool) -> str:
    from app.parsers.product_normalizer import (
        canonicalize_catalog_fields,
        sku_key_fields_strict,
    )

    if strict:
        fields = sku_key_fields_strict(
            model=row["model"],
            storage=row["storage"],
            color=row["color"],
            region=row["region"],
            sim=row["sim"],
        )
        return "|".join((fields[name] or "") for name in _FIELDS)
    model, storage = canonicalize_catalog_fields(row["model"], row["storage"])
    return "|".join(
        (
            model or "",
            storage or "",
            row["color"] or "",
            row["region"] or "",
            row["sim"] or "",
        )
    )


def _rewrite(*, strict: bool) -> None:
    conn = op.get_bind()
    last_id = 0
    while True:
        rows = (
            conn.execute(
                sa.text(
                    """
                    SELECT id, model, storage, color, region, sim, sku_key
                    FROM parsed_items
                    WHERE id > :last_id
                    ORDER BY id
                    LIMIT :batch
                    """
                ),
                {"last_id": last_id, "batch": _BATCH},
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        updates = []
        for row in rows:
            last_id = int(row["id"])
            new_key = _sku_key(row, strict=strict)
            if new_key != row["sku_key"]:
                updates.append({"id": last_id, "sku_key": new_key})
        if updates:
            conn.execute(
                sa.text("UPDATE parsed_items SET sku_key = :sku_key WHERE id = :id"),
                updates,
            )


def upgrade() -> None:
    _rewrite(strict=True)


def downgrade() -> None:
    _rewrite(strict=False)
