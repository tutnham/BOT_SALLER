"""Seed iPhone 18 markup rules (Plus/Pro +800).

Revision ID: 0021_iphone_18_markup_rules
Revises: 0020_supplier_message_items
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_iphone_18_markup_rules"
down_revision: str | None = "0020_supplier_message_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IPHONE_18_RULES: tuple[tuple[str, str, str, str, int], ...] = (
    ("iphone_18_pro_max", "apple", r"iphone\s+18\s+pro\s+max", "800", 105),
    ("iphone_18_pro", "apple", r"iphone\s+18\s+pro(?:\s|$)", "800", 104),
    ("iphone_18_plus", "apple", r"iphone\s+18\s+plus", "800", 103),
    ("iphone_18_base", "apple", r"iphone\s+18(?:\s|$)", "500", 102),
)


def upgrade() -> None:
    conn = op.get_bind()
    for rule_key, brand, pattern, markup, priority in _IPHONE_18_RULES:
        conn.execute(
            sa.text(
                """
                INSERT INTO markup_rules (
                    category, markup_fixed, active, rule_key, brand, model_pattern, priority
                )
                VALUES (:category, :markup, true, :rule_key, :brand, :pattern, :priority)
                ON CONFLICT (rule_key) DO UPDATE SET
                    markup_fixed = EXCLUDED.markup_fixed,
                    model_pattern = EXCLUDED.model_pattern,
                    priority = EXCLUDED.priority,
                    active = true,
                    updated_at = now()
                """
            ),
            {
                "category": rule_key,
                "markup": markup,
                "rule_key": rule_key,
                "brand": brand,
                "pattern": pattern,
                "priority": priority,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    for rule_key, _, _, _, _ in _IPHONE_18_RULES:
        conn.execute(
            sa.text("DELETE FROM markup_rules WHERE rule_key = :rule_key"),
            {"rule_key": rule_key},
        )
