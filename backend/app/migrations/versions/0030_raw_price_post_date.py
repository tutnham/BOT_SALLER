"""raw_prices.post_date: дата поста канала для агрегации «самая свежая цена».

Revision ID: 0030_raw_price_post_date
Revises: 0029_jev_decision_tokens
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_raw_price_post_date"
down_revision: str | None = "0029_jev_decision_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "raw_prices",
        sa.Column("post_date", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("raw_prices", "post_date")