"""supplier_message_items, quote_price_events, messages_in edit metadata.

Revision ID: 0020_supplier_message_items
Revises: 0019_messages_in_pending_binding_status
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_supplier_message_items"
down_revision: str | None = "0019_messages_in_pending_binding_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE quote_source ADD VALUE IF NOT EXISTS 'operator_corrected'")

    op.add_column(
        "messages_in",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "messages_in",
        sa.Column("raw_text_previous", sa.Text(), nullable=True),
    )

    op.create_table(
        "supplier_message_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_in_id", sa.BigInteger(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("parsed_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("parsed_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("parse_method", sa.Text(), nullable=True),
        sa.Column("bind_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("bind_method", sa.Text(), nullable=True),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column("conflict_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["message_in_id"], ["messages_in.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_in_id", "line_no", name="uq_supplier_message_items_line"),
    )
    op.create_index(
        "ix_supplier_message_items_pending",
        "supplier_message_items",
        ["bind_status"],
        postgresql_where=sa.text("bind_status = 'pending'"),
    )

    op.create_table(
        "quote_price_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("quote_id", sa.BigInteger(), nullable=False),
        sa.Column("old_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("new_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("message_in_id", sa.BigInteger(), nullable=True),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_in_id"], ["messages_in.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["item_id"], ["supplier_message_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("quote_price_events")
    op.drop_index("ix_supplier_message_items_pending", table_name="supplier_message_items")
    op.drop_table("supplier_message_items")
    op.drop_column("messages_in", "raw_text_previous")
    op.drop_column("messages_in", "deleted_at")
