"""Webhook inbox and Telegram outbox queues.

Revision ID: 0012_webhook_inbox_telegram_outbox
Revises: 0011_messages_in_binding
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_webhook_inbox_telegram_outbox"
down_revision: str | None = "0011_messages_in_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_inbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_update_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'dead')",
            name="ck_webhook_inbox_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tg_update_id", name="uq_webhook_inbox_tg_update_id"),
    )
    op.create_index(
        "ix_webhook_inbox_claim",
        "webhook_inbox",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "telegram_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("business_connection_id", sa.Text(), nullable=True),
        sa.Column("parse_mode", sa.Text(), nullable=True),
        sa.Column("reply_markup", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("message_out_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', 'dead', 'uncertain')",
            name="ck_telegram_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["message_out_id"],
            ["messages_out.id"],
            name="fk_telegram_outbox_message_out_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_telegram_outbox_dedupe_key"),
    )
    op.create_index(
        "ix_telegram_outbox_claim",
        "telegram_outbox",
        ["status", "next_attempt_at"],
    )

    op.drop_constraint("ck_messages_out_send_status", "messages_out", type_="check")
    op.create_check_constraint(
        "ck_messages_out_send_status",
        "messages_out",
        "send_status IN ('pending', 'sent', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_messages_out_send_status", "messages_out", type_="check")
    op.create_check_constraint(
        "ck_messages_out_send_status",
        "messages_out",
        "send_status IN ('sent', 'failed')",
    )
    op.drop_index("ix_telegram_outbox_claim", table_name="telegram_outbox")
    op.drop_table("telegram_outbox")
    op.drop_index("ix_webhook_inbox_claim", table_name="webhook_inbox")
    op.drop_table("webhook_inbox")
