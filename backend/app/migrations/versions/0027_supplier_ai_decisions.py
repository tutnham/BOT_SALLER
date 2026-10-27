"""Jev supplier classification audit table.

Revision ID: 0027_supplier_ai_decisions
Revises: 0026_deal_purchase_audit
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_supplier_ai_decisions"
down_revision: str | None = "0026_deal_purchase_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplier_ai_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("message_in_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("candidate_set_hash", sa.Text(), nullable=False),
        sa.Column("message_class", sa.Text(), nullable=True),
        sa.Column("price_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("selected_request_id", sa.BigInteger(), nullable=True),
        sa.Column("selected_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("second_probability", sa.Numeric(5, 4), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("recommended_action", sa.Text(), nullable=True),
        sa.Column("actual_action", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column(
            "shadow_mode",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("review_outcome", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["message_in_id"],
            ["messages_in.id"],
            name="fk_supplier_ai_decisions_message_in",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.id"],
            name="fk_supplier_ai_decisions_supplier",
        ),
        sa.ForeignKeyConstraint(
            ["selected_request_id"],
            ["requests.id"],
            name="fk_supplier_ai_decisions_request",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["employees.id"],
            name="fk_supplier_ai_decisions_reviewed_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_supplier_ai_decisions_message_in",
        "supplier_ai_decisions",
        ["message_in_id"],
    )
    op.create_index(
        "ix_supplier_ai_decisions_provider_created",
        "supplier_ai_decisions",
        ["provider", "created_at"],
    )
    op.create_index(
        "ix_supplier_ai_decisions_shadow_action",
        "supplier_ai_decisions",
        ["shadow_mode", "actual_action"],
    )
    op.create_index(
        "uq_supplier_ai_decisions_cache",
        "supplier_ai_decisions",
        [
            "message_in_id",
            "line_no",
            "input_hash",
            "candidate_set_hash",
            "model",
            "prompt_version",
            "schema_version",
        ],
        unique=True,
        postgresql_where=sa.text("error_code IS NULL AND superseded_at IS NULL"),
    )
    op.create_index(
        "ix_supplier_ai_decisions_review_pending",
        "supplier_ai_decisions",
        ["review_outcome"],
        postgresql_where=sa.text("review_outcome IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_ai_decisions_review_pending", table_name="supplier_ai_decisions")
    op.drop_index("uq_supplier_ai_decisions_cache", table_name="supplier_ai_decisions")
    op.drop_index("ix_supplier_ai_decisions_shadow_action", table_name="supplier_ai_decisions")
    op.drop_index("ix_supplier_ai_decisions_provider_created", table_name="supplier_ai_decisions")
    op.drop_index("ix_supplier_ai_decisions_message_in", table_name="supplier_ai_decisions")
    op.drop_table("supplier_ai_decisions")
