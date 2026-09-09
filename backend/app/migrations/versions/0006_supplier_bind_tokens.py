"""Add supplier_bind_tokens for owner-generated invite links.

Revision ID: 0006_supplier_bind_tokens
Revises: 0005_missing_fk_indexes
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_supplier_bind_tokens"
down_revision: str | None = "0005_missing_fk_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplier_bind_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "supplier_id",
            sa.BigInteger(),
            sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by_owner_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_supplier_bind_tokens_supplier_id",
        "supplier_bind_tokens",
        ["supplier_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_supplier_bind_tokens_supplier_id",
        table_name="supplier_bind_tokens",
    )
    op.drop_table("supplier_bind_tokens")
