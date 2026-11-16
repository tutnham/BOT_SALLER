"""Дата редактирования поста (B1: обработка EditedMessage).

Revision ID: 0006_post_edited_at
Revises: 0005_runtime_heartbeat
Create Date: 2026-10-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_post_edited_at"
down_revision: Union[str, None] = "0005_runtime_heartbeat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "parser_posts",
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("parser_posts", "edited_at")