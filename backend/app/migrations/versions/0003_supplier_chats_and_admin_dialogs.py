"""Add supplier_chats, pending_chats, admin_dialogs and rfq_enabled flag.

Revision ID: 0003_supplier_chats_and_admin_dialogs
Revises: 0002_indexes_and_constraints
Create Date: 2026-08-30
"""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

revision: str = "0003_supplier_chats_and_admin_dialogs"
down_revision: str | None = "0002_indexes_and_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


supplier_chat_type = sa.Enum(
    "private",
    "group",
    "supergroup",
    name="supplier_chat_type",
)


def upgrade() -> None:
    # Alembic default alembic_version.version_num is VARCHAR(32). This revision id is 37 chars.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")

    # Add supplier-level RFQ on/off toggle
    op.add_column(
        "suppliers",
        sa.Column("rfq_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    # New address book for supplier chats (groups or DMs)
    op.create_table(
        "supplier_chats",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("chat_type", supplier_chat_type, nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("bound_by_owner_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_supplier_chats_supplier_id", "supplier_chats", ["supplier_id"])
    op.create_index("ix_supplier_chats_active", "supplier_chats", ["active"])
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

    # Chats where the bot was added but owner has not classified yet
    op.create_table(
        "pending_chats",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("chat_type", supplier_chat_type, nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("invited_by_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # Owner FSM for multi-step admin dialogs
    op.create_table(
        "admin_dialogs",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '15 minutes'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # Audit field for client_groups
    op.add_column(
        "client_groups",
        sa.Column("bound_by_owner_id", sa.BigInteger(), nullable=True),
    )

    # Backfill private chats for existing suppliers so RFQ routing keeps working
    # after the migration.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO supplier_chats (supplier_id, chat_id, chat_type, title, is_default, active)
            SELECT id, telegram_id, 'private', name, true, true
            FROM suppliers
            WHERE telegram_id IS NOT NULL
              AND id NOT IN (
                  SELECT supplier_id FROM supplier_chats WHERE is_default = true
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("client_groups", "bound_by_owner_id")
    op.drop_table("admin_dialogs")
    op.drop_table("pending_chats")
    op.drop_constraint("uq_supplier_default_chat", "supplier_chats", type_="unique")
    op.drop_index("ix_supplier_chats_default", table_name="supplier_chats")
    op.drop_index("ix_supplier_chats_active", table_name="supplier_chats")
    op.drop_index("ix_supplier_chats_supplier_id", table_name="supplier_chats")
    op.drop_table("supplier_chats")
    op.drop_column("suppliers", "rfq_enabled")
    supplier_chat_type.drop(op.get_bind(), checkfirst=True)
