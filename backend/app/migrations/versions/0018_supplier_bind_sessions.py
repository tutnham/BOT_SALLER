"""Per-message supplier bind sessions (multi pending ambiguous prices).

Revision ID: 0018_supplier_bind_sessions
Revises: 0017_route_identity_partial_unique
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018_supplier_bind_sessions"
down_revision: str | None = "0017_route_identity_partial_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplier_bind_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("message_in_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("business_connection_id", sa.Text(), nullable=True),
        sa.Column(
            "candidate_request_ids",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("current_candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("resolution_method", sa.Text(), nullable=True),
        sa.Column("resolved_request_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by_telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_in_id"], ["messages_in.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_request_id"], ["requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_in_id", name="uq_supplier_bind_sessions_message_in_id"),
    )
    op.create_index(
        "ix_supplier_bind_sessions_supplier_status",
        "supplier_bind_sessions",
        ["supplier_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_bind_sessions_supplier_status", table_name="supplier_bind_sessions")
    op.drop_table("supplier_bind_sessions")
