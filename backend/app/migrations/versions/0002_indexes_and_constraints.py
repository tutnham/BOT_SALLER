"""Add quote uniqueness and hot-path indexes.

Revision ID: 0002_indexes_and_constraints
Revises: 0001_initial_schema
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_indexes_and_constraints"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Keep newest row per (request_id, supplier_id) before uniqueness.
    op.execute(
        """
        DELETE FROM quotes q
        USING quotes q2
        WHERE q.request_id = q2.request_id
          AND q.supplier_id = q2.supplier_id
          AND q.id < q2.id
        """
    )

    op.create_unique_constraint(
        "uq_quotes_request_supplier",
        "quotes",
        ["request_id", "supplier_id"],
    )

    op.create_index(
        "ix_requests_status_recheck_at",
        "requests",
        ["status", "recheck_at"],
    )
    op.create_index("ix_requests_created_at", "requests", ["created_at"])
    op.create_index("ix_quotes_request_id", "quotes", ["request_id"])
    op.create_index("ix_messages_out_supplier_id", "messages_out", ["supplier_id"])
    op.create_index("ix_raw_prices_received_at", "raw_prices", ["received_at"])
    op.create_index("ix_parsed_items_created_at", "parsed_items", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_parsed_items_created_at", table_name="parsed_items")
    op.drop_index("ix_raw_prices_received_at", table_name="raw_prices")
    op.drop_index("ix_messages_out_supplier_id", table_name="messages_out")
    op.drop_index("ix_quotes_request_id", table_name="quotes")
    op.drop_index("ix_requests_created_at", table_name="requests")
    op.drop_index("ix_requests_status_recheck_at", table_name="requests")
    op.drop_constraint("uq_quotes_request_supplier", "quotes", type_="unique")
