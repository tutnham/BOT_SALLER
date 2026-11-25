"""parsed_items.sku_key: каноническое имя iPhone в ключе (D7 deploy-day merge).

Строки, разобранные до канонизации моделей, хранят ключ с сырым именем
(«18 pro|512GB|…») и до конца суток дублируются в aggregate_today с новыми
строками («iPhone 18 Pro|512GB|…»). Пересчитываем ключи по текущей формуле.

sku_key необратим (старое имя не восстанавливается), downgrade правит только
шаблон правила iphone_13_16.

Revision ID: 0031_parsed_sku_key_canonical
Revises: 0030_raw_price_post_date
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_parsed_sku_key_canonical"
down_revision: str | None = "0030_raw_price_post_date"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_FAMILY_PATTERN = r"iphone\s+(?:1[3-6])(?:\s|$|\s+(?:pro|max|plus|mini|se|e)\b)"
_NEW_FAMILY_PATTERN = r"iphone\s+(?:1[3-6])(?:\s+(?!air\b)|$)"


def upgrade() -> None:
    from app.services.price_service import canonical_sku_key

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, model, storage, color, region, sim, sku_key FROM parsed_items"
        )
    ).mappings().all()
    updates = []
    for row in rows:
        new_key = canonical_sku_key(
            model=row["model"],
            storage=row["storage"],
            color=row["color"],
            region=row["region"],
            sim=row["sim"],
        )
        if new_key != row["sku_key"]:
            updates.append({"id": row["id"], "sku_key": new_key})
    if updates:
        conn.execute(
            sa.text("UPDATE parsed_items SET sku_key = :sku_key WHERE id = :id"),
            updates,
        )
    # Только сидированный шаблон; правку владельца не перетираем.
    conn.execute(
        sa.text(
            "UPDATE markup_rules SET model_pattern = :new_pattern, updated_at = now() "
            "WHERE rule_key = 'iphone_13_16' AND model_pattern = :old_pattern"
        ),
        {"new_pattern": _NEW_FAMILY_PATTERN, "old_pattern": _OLD_FAMILY_PATTERN},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE markup_rules SET model_pattern = :old_pattern, updated_at = now() "
            "WHERE rule_key = 'iphone_13_16' AND model_pattern = :new_pattern"
        ),
        {"old_pattern": _OLD_FAMILY_PATTERN, "new_pattern": _NEW_FAMILY_PATTERN},
    )
