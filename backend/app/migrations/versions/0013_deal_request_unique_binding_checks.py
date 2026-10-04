"""Unique deal per request and binding status checks.

Revision ID: 0013_deal_request_unique_binding_checks
Revises: 0012_webhook_inbox_telegram_outbox
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_deal_request_unique_binding_checks"
down_revision: str | None = "0012_webhook_inbox_telegram_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_deals_request_id",
        "deals",
        ["request_id"],
    )
    op.create_check_constraint(
        "ck_messages_in_bind_status",
        "messages_in",
        "bind_status IS NULL OR bind_status IN "
        "('bound', 'unbound', 'ignored')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_messages_in_bind_status", "messages_in", type_="check")
    op.drop_constraint("uq_deals_request_id", "deals", type_="unique")
