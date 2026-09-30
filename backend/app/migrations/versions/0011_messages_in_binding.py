"""Supplier reply binding audit columns and messages_out index.

Revision ID: 0011_messages_in_binding
Revises: 0010_supplier_categories
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_messages_in_binding"
down_revision: str | None = "0010_supplier_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages_in", sa.Column("bind_method", sa.Text(), nullable=True))
    op.add_column("messages_in", sa.Column("bind_status", sa.Text(), nullable=True))
    op.add_column("messages_in", sa.Column("bind_score", sa.REAL(), nullable=True))
    op.create_index(
        "ix_messages_out_supplier_request",
        "messages_out",
        ["supplier_id", "request_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_out_supplier_request", table_name="messages_out")
    op.drop_column("messages_in", "bind_score")
    op.drop_column("messages_in", "bind_status")
    op.drop_column("messages_in", "bind_method")
