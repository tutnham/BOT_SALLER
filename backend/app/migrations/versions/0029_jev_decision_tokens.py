"""Jev decision cost accounting: provider tokens and cache-hit marker.

Revision ID: 0029_jev_decision_tokens
Revises: 0028_price_publication
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_jev_decision_tokens"
down_revision: str | None = "0028_price_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "supplier_ai_decisions",
        sa.Column("input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "supplier_ai_decisions",
        sa.Column("output_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "supplier_ai_decisions",
        sa.Column(
            "from_cache",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("supplier_ai_decisions", "from_cache")
    op.drop_column("supplier_ai_decisions", "output_tokens")
    op.drop_column("supplier_ai_decisions", "input_tokens")