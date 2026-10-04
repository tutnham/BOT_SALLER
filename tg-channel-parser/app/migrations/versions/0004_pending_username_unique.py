"""Unique pending channel username (case-insensitive).

Revision ID: 0004_pending_username_unique
Revises: 0003_task_fk_and_posts_index
Create Date: 2026-10-04
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_pending_username_unique"
down_revision: Union[str, None] = "0003_task_fk_and_posts_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_parser_channels_pending_username
        ON parser_channels (lower(username))
        WHERE status = 'pending' AND username IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "uq_parser_channels_pending_username",
        table_name="parser_channels",
    )
