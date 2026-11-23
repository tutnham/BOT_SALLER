"""history_limit в parser_tasks: NULL = дефолт, 0 = вся история канала.

Revision ID: 0008_task_history_limit
Revises: 0007_backfill_task_type
Create Date: 2026-11-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_task_history_limit"
down_revision: Union[str, None] = "0007_backfill_task_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parser_tasks", sa.Column("history_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("parser_tasks", "history_limit")