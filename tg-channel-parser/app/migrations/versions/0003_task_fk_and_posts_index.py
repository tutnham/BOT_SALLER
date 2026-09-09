"""FK for parser_tasks.channel_id and list-posts index.

Revision ID: 0003_task_fk_and_posts_index
Revises: 0002_channel_resolution
Create Date: 2026-09-09
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_task_fk_and_posts_index"
down_revision: Union[str, None] = "0002_channel_resolution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_parser_tasks_channel_id",
        "parser_tasks",
        "parser_channels",
        ["channel_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_parser_posts_list",
        "parser_posts",
        ["channel_id", "content_type", "excluded", "post_date", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_parser_posts_list", table_name="parser_posts")
    op.drop_constraint(
        "fk_parser_tasks_channel_id",
        "parser_tasks",
        type_="foreignkey",
    )
