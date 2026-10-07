"""Deal purchase audit fields.

Revision ID: 0026_deal_purchase_audit
Revises: 0025_request_price_selections
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_deal_purchase_audit"
down_revision: str | None = "0025_request_price_selections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column("source_quote_id", sa.BigInteger(), nullable=True),
    )
    op.add_column("deals", sa.Column("purchased_qty", sa.Integer(), nullable=True))
    op.add_column(
        "deals",
        sa.Column("client_unit_price", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column("deals", sa.Column("override_reason", sa.Text(), nullable=True))
    op.add_column(
        "deals",
        sa.Column("recorded_by_employee_id", sa.BigInteger(), nullable=True),
    )
    op.add_column("deals", sa.Column("selection_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_deals_source_quote_id",
        "deals",
        "quotes",
        ["source_quote_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_deals_recorded_by_employee_id",
        "deals",
        "employees",
        ["recorded_by_employee_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_deals_selection_id",
        "deals",
        "request_price_selections",
        ["selection_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_deals_selection_id", "deals", type_="foreignkey")
    op.drop_constraint("fk_deals_recorded_by_employee_id", "deals", type_="foreignkey")
    op.drop_constraint("fk_deals_source_quote_id", "deals", type_="foreignkey")
    op.drop_column("deals", "selection_id")
    op.drop_column("deals", "recorded_by_employee_id")
    op.drop_column("deals", "override_reason")
    op.drop_column("deals", "client_unit_price")
    op.drop_column("deals", "purchased_qty")
    op.drop_column("deals", "source_quote_id")
