"""Runtime heartbeat for MTProto readiness.

Revision ID: 0005_runtime_heartbeat
Revises: 0004_pending_username_unique
Create Date: 2026-10-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_runtime_heartbeat"
down_revision: Union[str, None] = "0004_pending_username_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parser_runtime_heartbeats",
        sa.Column("instance_id", sa.Text(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_state", sa.Text(), nullable=False),
        sa.Column("last_channel_reload_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_parser_runtime_heartbeats_heartbeat_at",
        "parser_runtime_heartbeats",
        ["heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_parser_runtime_heartbeats_heartbeat_at")
    op.drop_table("parser_runtime_heartbeats")
