"""Supplier bind confirmation prompts (ambiguous price without reply).

Revision ID: 0014_supplier_bind_prompts
Revises: 0013_deal_request_unique_binding_checks
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014_supplier_bind_prompts"
down_revision: str | None = "0013_deal_request_unique_binding_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplier_bind_prompts",
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("message_in_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("pending_request_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("business_connection_id", sa.Text(), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_in_id"], ["messages_in.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("supplier_id"),
    )
    op.create_index(
        "ix_supplier_bind_prompts_message_in_id",
        "supplier_bind_prompts",
        ["message_in_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_bind_prompts_message_in_id", table_name="supplier_bind_prompts")
    op.drop_table("supplier_bind_prompts")
