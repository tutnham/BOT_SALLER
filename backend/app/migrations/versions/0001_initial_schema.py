"""Initial schema: all tables and ENUMs from TECHNICAL_DOCUMENTATION.md §6.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

request_status = postgresql.ENUM(
    "open",
    "awaiting_answers",
    "priced",
    "bargaining",
    "needs_recheck",
    "closed",
    "cancelled",
    name="request_status",
    create_type=False,
)
quote_source = postgresql.ENUM(
    "regex",
    "llm",
    "manual",
    name="quote_source",
    create_type=False,
)
deal_outcome = postgresql.ENUM(
    "won",
    "lost",
    "cancelled",
    name="deal_outcome",
    create_type=False,
)
message_kind = postgresql.ENUM(
    "ask",
    "bargain",
    "recheck",
    name="message_kind",
    create_type=False,
)


def upgrade() -> None:
    # ENUM types (DDL §6)
    op.execute(
        """
        CREATE TYPE request_status AS ENUM (
          'open', 'awaiting_answers', 'priced', 'bargaining',
          'needs_recheck', 'closed', 'cancelled'
        )
        """
    )
    op.execute("CREATE TYPE quote_source AS ENUM ('regex', 'llm', 'manual')")
    op.execute("CREATE TYPE deal_outcome AS ENUM ('won', 'lost', 'cancelled')")
    op.execute("CREATE TYPE message_kind AS ENUM ('ask', 'bargain', 'recheck')")

    op.create_table(
        "owners",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "dm_ok",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )

    op.create_table(
        "employees",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )

    op.create_table(
        "suppliers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "dm_ok",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("price_channel_id", sa.BigInteger(), nullable=True),
        sa.Column("price_channel_username", sa.Text(), nullable=True),
        sa.Column("last_price_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )

    op.create_table(
        "client_groups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id"),
    )

    op.create_table(
        "markup_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("markup_fixed", sa.Numeric(12, 2), nullable=True),
        sa.Column("markup_min", sa.Numeric(12, 2), nullable=True),
        sa.Column("markup_max", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "parse_cache",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
    )

    op.create_table(
        "report_cache",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("formatted_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period_key"),
    )

    op.create_table(
        "update_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_update_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tg_update_id"),
    )

    op.create_table(
        "requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("group_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column(
            "normalized_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "status",
            request_status,
            server_default=sa.text("'open'::request_status"),
            nullable=False,
        ),
        sa.Column("recheck_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "price_list_drafts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["approved_by"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "messages_out",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", message_kind, nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "tg_message_id"),
    )

    op.create_table(
        "messages_in",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "tg_message_id"),
    )

    op.create_table(
        "quotes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("price_initial", sa.Numeric(12, 2), nullable=True),
        sa.Column("price_bargain", sa.Numeric(12, 2), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("source", quote_source, nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=True),
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
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "deals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("chosen_supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("final_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("outcome", deal_outcome, nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chosen_supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "raw_prices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "source",
            sa.Text(),
            server_default=sa.text("'manual_message'"),
            nullable=False,
        ),
        sa.Column("source_post_id", sa.BigInteger(), nullable=True),
        sa.Column("source_message_link", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
    )

    op.create_table(
        "parsed_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("raw_price_id", sa.BigInteger(), nullable=False),
        sa.Column("sku_key", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("storage", sa.Text(), nullable=True),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("sim", sa.Text(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "currency",
            sa.Text(),
            server_default=sa.text("'RUB'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["raw_price_id"], ["raw_prices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("parsed_items")
    op.drop_table("raw_prices")
    op.drop_table("deals")
    op.drop_table("quotes")
    op.drop_table("messages_in")
    op.drop_table("messages_out")
    op.drop_table("price_list_drafts")
    op.drop_table("requests")
    op.drop_table("update_log")
    op.drop_table("report_cache")
    op.drop_table("parse_cache")
    op.drop_table("markup_rules")
    op.drop_table("client_groups")
    op.drop_table("suppliers")
    op.drop_table("employees")
    op.drop_table("owners")

    op.execute("DROP TYPE IF EXISTS message_kind")
    op.execute("DROP TYPE IF EXISTS deal_outcome")
    op.execute("DROP TYPE IF EXISTS quote_source")
    op.execute("DROP TYPE IF EXISTS request_status")
