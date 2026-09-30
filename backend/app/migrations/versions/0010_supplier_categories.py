"""Supplier product categories (many-to-many).

Revision ID: 0010_supplier_categories
Revises: 0009_markup_rules_v2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_supplier_categories"
down_revision: str | None = "0009_markup_rules_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORIES = ("apple", "samsung", "power_station", "other")


def upgrade() -> None:
    op.create_table(
        "supplier_categories",
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("supplier_id", "category"),
        sa.CheckConstraint(
            "category IN ('apple', 'samsung', 'power_station', 'other')",
            name="ck_supplier_categories_category",
        ),
    )
    conn = op.get_bind()
    supplier_ids = conn.execute(sa.text("SELECT id FROM suppliers")).scalars().all()
    for supplier_id in supplier_ids:
        for category in _CATEGORIES:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO supplier_categories (supplier_id, category)
                    VALUES (:supplier_id, :category)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"supplier_id": supplier_id, "category": category},
            )


def downgrade() -> None:
    op.drop_table("supplier_categories")
