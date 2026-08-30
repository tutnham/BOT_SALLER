"""Add channel resolution lifecycle and resolve_channel task type.

Revision ID: 0002_channel_resolution
Revises: 0001_initial
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_channel_resolution"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

parser_channel_status = postgresql.ENUM(
    "pending",
    "active",
    "failed",
    name="parser_channel_status",
    create_type=False,
)


def upgrade() -> None:
    # Add resolve_channel to parser_task_type
    op.execute("ALTER TYPE parser_task_type ADD VALUE IF NOT EXISTS 'resolve_channel'")

    # Channel lifecycle: registration can be pending until MTProto resolves it.
    parser_channel_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "parser_channels",
        sa.Column(
            "status",
            parser_channel_status,
            server_default=sa.text("'active'"),
            nullable=False,
        ),
    )
    op.add_column(
        "parser_channels",
        sa.Column("error_text", sa.Text(), nullable=True),
    )

    # Old registrations without explicit status were already active.
    op.execute("UPDATE parser_channels SET status = 'active' WHERE status = 'pending'")

    # New channels may not have a numeric id yet (pending resolution).
    op.alter_column("parser_channels", "channel_id", nullable=True)

    # Partial unique constraint for known channel ids.
    op.drop_constraint("parser_channels_channel_id_key", "parser_channels", type_="unique")
    op.create_index(
        "ix_parser_channels_channel_id",
        "parser_channels",
        ["channel_id"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )

    # Tasks may reference a channel without a post (for resolution).
    op.alter_column("parser_tasks", "post_id", nullable=True)
    op.add_column(
        "parser_tasks",
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "idx_parser_tasks_resolve_channel",
        "parser_tasks",
        ["status", "task_type", "scheduled_at"],
        postgresql_where=sa.text("task_type = 'resolve_channel'"),
    )


def downgrade() -> None:
    op.drop_index("idx_parser_tasks_resolve_channel", table_name="parser_tasks")
    op.drop_column("parser_tasks", "channel_id")
    op.alter_column("parser_tasks", "post_id", nullable=False)

    op.drop_index("ix_parser_channels_channel_id", table_name="parser_channels")
    op.create_unique_constraint(
        "parser_channels_channel_id_key",
        "parser_channels",
        ["channel_id"],
    )
    op.alter_column("parser_channels", "channel_id", nullable=False)

    op.drop_column("parser_channels", "error_text")
    op.drop_column("parser_channels", "status")
    parser_channel_status.drop(op.get_bind(), checkfirst=True)
