"""Indexes for admin_audit_log lookups.

Revision ID: 0016_admin_audit_indexes
Revises: 0015_ops_heartbeat_audit
Create Date: 2026-10-05
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0016_admin_audit_indexes"
down_revision: Union[str, None] = "0015_ops_heartbeat_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_admin_audit_log_action", "admin_audit_log", ["action"])
    op.create_index(
        "ix_admin_audit_log_entity",
        "admin_audit_log",
        ["entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_log_entity", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_log_action", table_name="admin_audit_log")
