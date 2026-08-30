"""initial parser schema (doc §9)

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

parser_content_type = postgresql.ENUM(
    "text",
    "photo",
    "video",
    "document",
    "audio",
    "voice",
    "media_group",
    "other",
    name="parser_content_type",
    create_type=False,
)
parser_status = postgresql.ENUM(
    "new",
    "downloaded",
    "processing",
    "processed",
    "failed",
    name="parser_status",
    create_type=False,
)
parser_task_type = postgresql.ENUM(
    "download_media",
    "ai_process",
    name="parser_task_type",
    create_type=False,
)
parser_media_type = postgresql.ENUM(
    "photo",
    "video",
    "document",
    "audio",
    "voice",
    name="parser_media_type",
    create_type=False,
)
parser_storage_backend = postgresql.ENUM(
    "local",
    "s3",
    name="parser_storage_backend",
    create_type=False,
)
parser_channel_purpose = postgresql.ENUM(
    "monitoring",
    "supplier_price_source",
    name="parser_channel_purpose",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    parser_content_type.create(bind, checkfirst=True)
    parser_status.create(bind, checkfirst=True)
    parser_task_type.create(bind, checkfirst=True)
    parser_media_type.create(bind, checkfirst=True)
    parser_storage_backend.create(bind, checkfirst=True)
    parser_channel_purpose.create(bind, checkfirst=True)

    op.create_table(
        "parser_channels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "purpose",
            parser_channel_purpose,
            server_default=sa.text("'monitoring'"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_synced_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id"),
    )

    op.create_table(
        "parser_posts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("grouped_id", sa.BigInteger(), nullable=True),
        sa.Column("post_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_type", parser_content_type, nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("message_link", sa.Text(), nullable=True),
        sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            parser_status,
            server_default=sa.text("'new'"),
            nullable=False,
        ),
        sa.Column("excluded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["channel_id"], ["parser_channels.channel_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "message_id", name="uq_parser_posts_channel_message"),
    )

    op.create_table(
        "parser_media_files",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("media_type", parser_media_type, nullable=False),
        sa.Column("file_unique_id", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column("storage_backend", parser_storage_backend, nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("sha256_hash", sa.Text(), nullable=True),
        sa.Column(
            "download_status",
            parser_status,
            server_default=sa.text("'new'"),
            nullable=False,
        ),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["post_id"], ["parser_posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "file_unique_id", name="uq_parser_media_post_file"),
    )

    op.create_table(
        "parser_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("task_type", parser_task_type, nullable=False),
        sa.Column(
            "status",
            parser_status,
            server_default=sa.text("'new'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["post_id"], ["parser_posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_parser_tasks_pickup", "parser_tasks", ["status", "scheduled_at"])

    op.create_table(
        "parser_ai_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("key_topics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("toxicity_score", sa.REAL(), nullable=True),
        sa.Column("has_profanity", sa.Boolean(), nullable=True),
        sa.Column("cleaned_text", sa.Text(), nullable=True),
        sa.Column("classification_reason", sa.Text(), nullable=True),
        sa.Column("schema_valid", sa.Boolean(), nullable=False),
        sa.Column("model_used", sa.Text(), nullable=False),
        sa.Column("raw_ai_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["post_id"], ["parser_posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_parser_ai_results_post", "parser_ai_results", ["post_id", "is_current"]
    )


def downgrade() -> None:
    op.drop_index("idx_parser_ai_results_post", table_name="parser_ai_results")
    op.drop_table("parser_ai_results")
    op.drop_index("idx_parser_tasks_pickup", table_name="parser_tasks")
    op.drop_table("parser_tasks")
    op.drop_table("parser_media_files")
    op.drop_table("parser_posts")
    op.drop_table("parser_channels")

    bind = op.get_bind()
    parser_channel_purpose.drop(bind, checkfirst=True)
    parser_storage_backend.drop(bind, checkfirst=True)
    parser_media_type.drop(bind, checkfirst=True)
    parser_task_type.drop(bind, checkfirst=True)
    parser_status.drop(bind, checkfirst=True)
    parser_content_type.drop(bind, checkfirst=True)
