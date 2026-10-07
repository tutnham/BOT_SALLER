"""Daily SKU best-price projection.

Revision ID: 0024_daily_sku_prices
Revises: 0023_supplier_rfq_groups
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_daily_sku_prices"
down_revision: str | None = "0023_supplier_rfq_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_sku_prices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("business_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_sku_key", sa.Text(), nullable=False),
        sa.Column("source_quote_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("purchase_unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("client_unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("available_qty", sa.Integer(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column("selection_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_quote_id"], ["quotes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_date",
            "canonical_sku_key",
            name="uq_daily_sku_prices_date_key",
        ),
        sa.CheckConstraint(
            "purchase_unit_price >= 0 AND client_unit_price >= 0",
            name="ck_daily_sku_prices_nonneg",
        ),
    )


def downgrade() -> None:
    op.drop_table("daily_sku_prices")
