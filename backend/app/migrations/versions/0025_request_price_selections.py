"""Versioned price selections per request.

Revision ID: 0025_request_price_selections
Revises: 0024_daily_sku_prices
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_request_price_selections"
down_revision: str | None = "0024_daily_sku_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "request_price_selections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=True),
        sa.Column("canonical_sku_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("selected_quote_id", sa.BigInteger(), nullable=True),
        sa.Column("selected_supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("purchase_unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("requested_qty", sa.Integer(), nullable=True),
        sa.Column("client_unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=True),
        sa.Column("rejected_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("selection_version", sa.Integer(), nullable=False),
        sa.Column(
            "selected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["request_batches.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["selected_quote_id"], ["quotes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["selected_supplier_id"], ["suppliers.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "purchase_unit_price IS NULL OR purchase_unit_price >= 0",
            name="ck_request_price_selections_purchase_nonneg",
        ),
        sa.CheckConstraint(
            "client_unit_price IS NULL OR client_unit_price >= 0",
            name="ck_request_price_selections_client_nonneg",
        ),
    )
    op.create_index(
        "ix_request_price_selections_request_id",
        "request_price_selections",
        ["request_id"],
    )
    op.create_index(
        "uq_request_price_selections_published",
        "request_price_selections",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_table("request_price_selections")
