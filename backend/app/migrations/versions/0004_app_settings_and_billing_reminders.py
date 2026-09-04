"""Add runtime app_settings and monthly billing_reminders idempotency log.

Revision ID: 0004_app_settings_and_billing_reminders
Revises: 0003_supplier_chats_and_admin_dialogs
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_app_settings_and_billing_reminders"
down_revision: str | None = "0003_supplier_chats_and_admin_dialogs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Runtime-mutable configuration (e.g. LLM top-up price text editable by owner).
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            sa.ForeignKey("owners.id"),
            nullable=True,
        ),
    )

    # Idempotency log for periodic billing reminders by (kind, period_key).
    op.create_table(
        "billing_reminders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "chat_ids",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_billing_reminders_kind_period_key",
        "billing_reminders",
        ["kind", "period_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_reminders_kind_period_key",
        table_name="billing_reminders",
    )
    op.drop_table("billing_reminders")
    op.drop_table("app_settings")
