"""request_batches and batch fields on requests.

Revision ID: 0022_request_batches
Revises: 0021_iphone_18_markup_rules
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_request_batches"
down_revision: str | None = "0021_iphone_18_markup_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "request_batches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("group_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("items_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_priced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_unresolved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
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
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selection_version", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft', 'awaiting_confirmation', 'awaiting_quotes', "
            "'partially_priced', 'ready', 'published', 'closed', 'cancelled')",
            name="ck_request_batches_status",
        ),
    )
    op.create_index("ix_request_batches_employee_id", "request_batches", ["employee_id"])

    op.add_column("requests", sa.Column("batch_id", sa.BigInteger(), nullable=True))
    op.add_column("requests", sa.Column("line_no", sa.Integer(), nullable=True))
    op.add_column("requests", sa.Column("source_line", sa.Text(), nullable=True))
    op.add_column("requests", sa.Column("canonical_sku_key", sa.Text(), nullable=True))
    op.add_column("requests", sa.Column("normalizer_version", sa.Text(), nullable=True))
    op.add_column("requests", sa.Column("normalization_confidence", sa.REAL(), nullable=True))
    op.add_column("requests", sa.Column("requested_qty", sa.Integer(), nullable=True))
    op.add_column("requests", sa.Column("price_state", sa.Text(), nullable=True))
    op.add_column(
        "requests",
        sa.Column("quote_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "requests",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_foreign_key(
        "fk_requests_batch_id",
        "requests",
        "request_batches",
        ["batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_requests_batch_id", "requests", ["batch_id"])
    op.create_index(
        "uq_requests_batch_line_no",
        "requests",
        ["batch_id", "line_no"],
        unique=True,
        postgresql_where=sa.text("batch_id IS NOT NULL AND line_no IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_requests_normalization_confidence",
        "requests",
        "normalization_confidence IS NULL OR "
        "(normalization_confidence >= 0 AND normalization_confidence <= 1)",
    )
    op.create_check_constraint(
        "ck_requests_requested_qty_positive",
        "requests",
        "requested_qty IS NULL OR requested_qty > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_requests_requested_qty_positive", "requests", type_="check")
    op.drop_constraint("ck_requests_normalization_confidence", "requests", type_="check")
    op.drop_index("uq_requests_batch_line_no", table_name="requests")
    op.drop_index("ix_requests_batch_id", table_name="requests")
    op.drop_constraint("fk_requests_batch_id", "requests", type_="foreignkey")
    op.drop_column("requests", "version")
    op.drop_column("requests", "quote_deadline_at")
    op.drop_column("requests", "price_state")
    op.drop_column("requests", "requested_qty")
    op.drop_column("requests", "normalization_confidence")
    op.drop_column("requests", "normalizer_version")
    op.drop_column("requests", "canonical_sku_key")
    op.drop_column("requests", "source_line")
    op.drop_column("requests", "line_no")
    op.drop_column("requests", "batch_id")
    op.drop_table("request_batches")
