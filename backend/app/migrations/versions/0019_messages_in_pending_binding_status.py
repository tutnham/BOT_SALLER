"""Allow pending_binding on messages_in.bind_status.

Revision ID: 0019_messages_in_pending_binding_status
Revises: 0018_supplier_bind_sessions
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_messages_in_pending_binding_status"
down_revision: str | None = "0018_supplier_bind_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_messages_in_bind_status", "messages_in", type_="check")
    op.create_check_constraint(
        "ck_messages_in_bind_status",
        "messages_in",
        "bind_status IS NULL OR bind_status IN "
        "('bound', 'unbound', 'ignored', 'pending_binding')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_messages_in_bind_status", "messages_in", type_="check")
    op.create_check_constraint(
        "ck_messages_in_bind_status",
        "messages_in",
        "bind_status IS NULL OR bind_status IN ('bound', 'unbound', 'ignored')",
    )
