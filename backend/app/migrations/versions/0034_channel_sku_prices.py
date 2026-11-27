"""channel_sku_prices: цены прайс-каналов в канонических ключах заявок.

Проекция пересобирается из parsed_items и даёт пути заявки цену, уже снятую с
каналов прайса: ключ здесь тот же ``v1|apple|iphone|...``, что и в
``requests.canonical_sku_key`` (parsed_items.sku_key — другое пространство).
Поставщиков и котировок не изобретает: ``quotes`` остаётся только про RFQ.

Revision ID: 0034_channel_sku_prices
Revises: 0033_price_draft_review_sources
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_channel_sku_prices"
down_revision: str | None = "0033_price_draft_review_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_sku_prices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("business_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_sku_key", sa.Text(), nullable=False),
        sa.Column("sku_key", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("storage", sa.Text(), nullable=True),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("sim", sa.Text(), nullable=True),
        sa.Column("purchase_unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("client_unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("markup_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("markup_rule_id", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'RUB'")),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("parsed_item_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column("selection_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(
            ["markup_rule_id"], ["markup_rules.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parsed_item_id"], ["parsed_items.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_date",
            "canonical_sku_key",
            "currency",
            name="uq_channel_sku_prices_date_key_currency",
        ),
        sa.CheckConstraint(
            "purchase_unit_price >= 0 AND client_unit_price >= 0 AND markup_rub >= 0",
            name="ck_channel_sku_prices_nonneg",
        ),
    )
    op.create_index(
        "ix_channel_sku_prices_canonical_sku_key", "channel_sku_prices", ["canonical_sku_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_channel_sku_prices_canonical_sku_key", table_name="channel_sku_prices")
    op.drop_table("channel_sku_prices")
