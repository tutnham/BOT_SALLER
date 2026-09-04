"""Add missing foreign-key indexes used by hot-path queries.

Revision ID: 0005_missing_fk_indexes
Revises: 0004_app_settings_and_billing_reminders
Create Date: 2026-09-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_missing_fk_indexes"
down_revision: str | None = "0004_app_settings_and_billing_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_requests_employee_id", "requests", ["employee_id"])
    op.create_index("ix_messages_in_request_id", "messages_in", ["request_id"])
    op.create_index("ix_messages_in_supplier_id", "messages_in", ["supplier_id"])
    op.create_index("ix_messages_out_request_id", "messages_out", ["request_id"])
    op.create_index("ix_deals_request_id", "deals", ["request_id"])
    op.create_index("ix_deals_chosen_supplier_id", "deals", ["chosen_supplier_id"])
    op.create_index("ix_raw_prices_supplier_id", "raw_prices", ["supplier_id"])
    op.create_index("ix_parsed_items_raw_price_id", "parsed_items", ["raw_price_id"])
    op.create_index("ix_app_settings_updated_by", "app_settings", ["updated_by"])


def downgrade() -> None:
    op.drop_index("ix_app_settings_updated_by", table_name="app_settings")
    op.drop_index("ix_parsed_items_raw_price_id", table_name="parsed_items")
    op.drop_index("ix_raw_prices_supplier_id", table_name="raw_prices")
    op.drop_index("ix_deals_chosen_supplier_id", table_name="deals")
    op.drop_index("ix_deals_request_id", table_name="deals")
    op.drop_index("ix_messages_out_request_id", table_name="messages_out")
    op.drop_index("ix_messages_in_supplier_id", table_name="messages_in")
    op.drop_index("ix_messages_in_request_id", table_name="messages_in")
    op.drop_index("ix_requests_employee_id", table_name="requests")
