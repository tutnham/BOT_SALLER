"""Grouped supplier RFQ message mapping.

Revision ID: 0023_supplier_rfq_groups
Revises: 0022_request_batches
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_supplier_rfq_groups"
down_revision: str | None = "0022_request_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "supplier_rfq_batches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("business_connection_id", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["request_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_supplier_rfq_batches_batch_id", "supplier_rfq_batches", ["batch_id"]
    )
    op.create_table(
        "supplier_rfq_batch_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rfq_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("line_code", sa.Text(), nullable=False),
        sa.Column("message_out_id", sa.BigInteger(), nullable=True),
        sa.Column("delivery_status", sa.Text(), nullable=False, server_default="pending"),
        sa.ForeignKeyConstraint(
            ["rfq_batch_id"], ["supplier_rfq_batches.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_out_id"], ["messages_out.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rfq_batch_id",
            "request_id",
            name="uq_supplier_rfq_batch_items_batch_request",
        ),
    )
    op.create_index(
        "ix_supplier_rfq_batch_items_message_out_id",
        "supplier_rfq_batch_items",
        ["message_out_id"],
    )


def downgrade() -> None:
    op.drop_table("supplier_rfq_batch_items")
    op.drop_table("supplier_rfq_batches")
