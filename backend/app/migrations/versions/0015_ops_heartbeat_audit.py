"""Process heartbeats, job runs, admin audit, and queue resolution columns.

Revision ID: 0015_ops_heartbeat_audit
Revises: 0014_supplier_bind_prompts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015_ops_heartbeat_audit"
down_revision: str | None = "0014_supplier_bind_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "process_heartbeats",
        sa.Column("process_type", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("app_version", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("process_type", "instance_id", name="pk_process_heartbeats"),
    )
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
    )
    op.create_index("ix_job_runs_finished_at", "job_runs", ["finished_at"])
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("previous_state", JSONB(), nullable=True),
        sa.Column("new_state", JSONB(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_audit_log"),
    )
    op.create_index("ix_admin_audit_log_created_at", "admin_audit_log", ["created_at"])
    for table in ("webhook_inbox", "telegram_outbox"):
        op.add_column(table, sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("resolved_by", sa.BigInteger(), nullable=True))
        op.add_column(table, sa.Column("resolution_reason", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("replay_of_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    for table in ("telegram_outbox", "webhook_inbox"):
        op.drop_column(table, "replay_of_id")
        op.drop_column(table, "resolution_reason")
        op.drop_column(table, "resolved_by")
        op.drop_column(table, "resolved_at")
    op.drop_index("ix_admin_audit_log_created_at", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
    op.drop_index("ix_job_runs_finished_at", table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_table("process_heartbeats")
