"""Telegram Business connections and business_dm chat type.

Revision ID: 0008_business_connections
Revises: 0007_price_approval_and_supplier_chats
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_business_connections"
down_revision: str | None = "0007_price_approval_and_supplier_chats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RLS_UP = """
DO $$
BEGIN
  ALTER TABLE business_connections ENABLE ROW LEVEL SECURITY;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON TABLE business_connections FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON TABLE business_connections FROM authenticated;
  END IF;
END
$$;
"""

_RLS_DOWN = """
DO $$
BEGIN
  IF to_regclass('public.business_connections') IS NOT NULL THEN
    ALTER TABLE business_connections DISABLE ROW LEVEL SECURITY;
  END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute("ALTER TYPE supplier_chat_type ADD VALUE IF NOT EXISTS 'business_dm'")

    op.create_table(
        "business_connections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("business_connection_id", sa.Text(), nullable=False),
        sa.Column(
            "can_reply",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "can_read_messages",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("raw_rights", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tg_user_id"),
        sa.UniqueConstraint("business_connection_id"),
    )
    op.execute(_RLS_UP)

    op.add_column(
        "supplier_chats",
        sa.Column("business_connection_id", sa.Text(), nullable=True),
    )
    op.drop_constraint("supplier_chats_chat_id_key", "supplier_chats", type_="unique")
    op.create_unique_constraint(
        "uq_supplier_chats_chat_id_type",
        "supplier_chats",
        ["chat_id", "chat_type"],
    )
    op.create_index(
        "ix_supplier_chats_business_connection_id",
        "supplier_chats",
        ["business_connection_id"],
    )

    op.add_column(
        "messages_out",
        sa.Column("business_connection_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "messages_out",
        sa.Column(
            "send_status",
            sa.String(length=20),
            server_default=sa.text("'sent'"),
            nullable=False,
        ),
    )
    op.add_column("messages_out", sa.Column("error_text", sa.Text(), nullable=True))
    op.alter_column("messages_out", "tg_message_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        "ck_messages_out_send_status",
        "messages_out",
        "send_status IN ('sent', 'failed')",
    )

    op.add_column(
        "messages_in",
        sa.Column("business_connection_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages_in", "business_connection_id")

    op.execute("DELETE FROM messages_out WHERE send_status = 'failed' OR tg_message_id IS NULL")
    op.drop_constraint("ck_messages_out_send_status", "messages_out", type_="check")
    op.drop_column("messages_out", "error_text")
    op.drop_column("messages_out", "send_status")
    op.drop_column("messages_out", "business_connection_id")
    op.alter_column(
        "messages_out",
        "tg_message_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )

    op.execute(
        """
        DELETE FROM supplier_chats
        WHERE chat_type::text = 'business_dm'
        """
    )
    op.drop_index("ix_supplier_chats_business_connection_id", table_name="supplier_chats")
    op.drop_constraint("uq_supplier_chats_chat_id_type", "supplier_chats", type_="unique")
    op.create_unique_constraint("supplier_chats_chat_id_key", "supplier_chats", ["chat_id"])
    op.drop_column("supplier_chats", "business_connection_id")

    op.execute(_RLS_DOWN)
    op.drop_table("business_connections")

    op.execute("ALTER TYPE supplier_chat_type RENAME TO supplier_chat_type_old")
    op.execute("CREATE TYPE supplier_chat_type AS ENUM ('private', 'group', 'supergroup')")
    op.execute(
        """
        ALTER TABLE supplier_chats
        ALTER COLUMN chat_type TYPE supplier_chat_type
        USING chat_type::text::supplier_chat_type
        """
    )
    op.execute(
        """
        ALTER TABLE pending_chats
        ALTER COLUMN chat_type TYPE supplier_chat_type
        USING chat_type::text::supplier_chat_type
        """
    )
    op.execute("DROP TYPE supplier_chat_type_old")
