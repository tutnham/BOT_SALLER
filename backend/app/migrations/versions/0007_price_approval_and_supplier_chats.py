"""Price approval audit + fix supplier default chat uniqueness.

Revision ID: 0007_price_approval_and_supplier_chats
Revises: 0006_supplier_bind_tokens
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_price_approval_and_supplier_chats"
down_revision: str | None = "0006_supplier_bind_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_list_drafts",
        sa.Column("approved_by_telegram_id", sa.BigInteger(), nullable=True),
    )

    # Old constraint allowed at most two chats per supplier (one default, one not).
    op.drop_constraint("uq_supplier_default_chat", "supplier_chats", type_="unique")
    op.drop_index("ix_supplier_chats_default", table_name="supplier_chats")
    op.create_index(
        "ix_supplier_chats_default",
        "supplier_chats",
        ["supplier_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_chats_default", table_name="supplier_chats")
    op.create_index(
        "ix_supplier_chats_default",
        "supplier_chats",
        ["supplier_id"],
        postgresql_where=sa.text("is_default = true"),
    )
    op.create_unique_constraint(
        "uq_supplier_default_chat",
        "supplier_chats",
        ["supplier_id", "is_default"],
    )
    op.drop_column("price_list_drafts", "approved_by_telegram_id")
