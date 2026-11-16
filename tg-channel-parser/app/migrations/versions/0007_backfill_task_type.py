"""Тип задачи backfill_channel (B4: повторный backfill после сбоя).

Revision ID: 0007_backfill_task_type
Revises: 0006_post_edited_at
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0007_backfill_task_type"
down_revision: Union[str, None] = "0006_post_edited_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG12+: ADD VALUE допустим в транзакции, если значение не используется до commit
    op.execute("ALTER TYPE parser_task_type ADD VALUE IF NOT EXISTS 'backfill_channel'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значений enum; значение остаётся неиспользуемым
    pass