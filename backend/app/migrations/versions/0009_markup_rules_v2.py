"""Markup rules v2 (pattern/priority) and quote markup audit columns.

Revision ID: 0009_markup_rules_v2
Revises: 0008_business_connections
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_markup_rules_v2"
down_revision: str | None = "0008_business_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CUSTOMER_RULES: tuple[tuple[str, str, str, str, int], ...] = (
    # rule_key, brand, model_pattern (normalized text), markup_rub, priority
    ("iphone_17_pro_max", "apple", r"iphone\s+17\s+pro\s+max", "800", 100),
    ("iphone_17_pro", "apple", r"iphone\s+17\s+pro(?:\s|$)", "800", 99),
    ("iphone_17_air", "apple", r"iphone\s+17\s+air", "500", 98),
    ("iphone_17_e", "apple", r"iphone\s+17\s*e(?:\s|$)", "500", 97),
    ("iphone_17_base", "apple", r"iphone\s+17(?:\s|$)", "500", 96),
    ("iphone_16_pro_max", "apple", r"iphone\s+16\s+pro\s+max", "800", 95),
    ("iphone_13_16", "apple", r"iphone\s+(?:1[3-6])(?:\s|$|\s+(?:pro|max|plus|mini|se|e)\b)", "500", 90),
    ("ipad", "apple", r"\bipad\b", "500", 80),
    ("apple_watch", "apple", r"(?:apple\s*watch|iwatch)\b", "500", 79),
    ("airpods", "apple", r"\bairpods?\b", "500", 78),
    ("macbook", "apple", r"\bmacbook\b", "1000", 77),
    ("samsung", "samsung", r"(?:samsung|galaxy)\b", "800", 70),
)


def upgrade() -> None:
    op.add_column("markup_rules", sa.Column("rule_key", sa.Text(), nullable=True))
    op.add_column("markup_rules", sa.Column("brand", sa.Text(), nullable=True))
    op.add_column("markup_rules", sa.Column("model_pattern", sa.Text(), nullable=True))
    op.add_column("markup_rules", sa.Column("priority", sa.Integer(), nullable=True))
    op.add_column(
        "markup_rules",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    op.create_unique_constraint("uq_markup_rules_rule_key", "markup_rules", ["rule_key"])

    op.add_column("quotes", sa.Column("markup_rub", sa.Numeric(12, 2), nullable=True))
    op.add_column("quotes", sa.Column("price_final", sa.Numeric(12, 2), nullable=True))
    op.add_column("quotes", sa.Column("markup_rule_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_quotes_markup_rule_id",
        "quotes",
        "markup_rules",
        ["markup_rule_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute("UPDATE markup_rules SET active = false WHERE rule_key IS NULL")

    conn = op.get_bind()
    for rule_key, brand, pattern, markup, priority in _CUSTOMER_RULES:
        conn.execute(
            sa.text(
                """
                INSERT INTO markup_rules (
                    category, markup_fixed, active, rule_key, brand, model_pattern, priority
                )
                VALUES (:category, :markup, true, :rule_key, :brand, :pattern, :priority)
                ON CONFLICT (rule_key) DO NOTHING
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
    for rule_key, _, _, _, _ in _CUSTOMER_RULES:
        conn.execute(
            sa.text("DELETE FROM markup_rules WHERE rule_key = :rule_key"),
            {"rule_key": rule_key},
        )

    op.drop_constraint("fk_quotes_markup_rule_id", "quotes", type_="foreignkey")
    op.drop_column("quotes", "markup_rule_id")
    op.drop_column("quotes", "price_final")
    op.drop_column("quotes", "markup_rub")

    op.drop_constraint("uq_markup_rules_rule_key", "markup_rules", type_="unique")
    op.drop_column("markup_rules", "updated_at")
    op.drop_column("markup_rules", "priority")
    op.drop_column("markup_rules", "model_pattern")
    op.drop_column("markup_rules", "brand")
    op.drop_column("markup_rules", "rule_key")

    op.execute("UPDATE markup_rules SET active = true WHERE category IS NOT NULL")
