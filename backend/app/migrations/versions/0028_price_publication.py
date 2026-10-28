"""Quote observation timestamp and price publication audit columns.

Revision ID: 0028_price_publication
Revises: 0027_supplier_ai_decisions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_price_publication"
down_revision: str | None = "0027_supplier_ai_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quotes",
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute("UPDATE quotes SET observed_at = created_at")

    op.add_column("request_price_selections", sa.Column("idempotency_key", sa.Text()))
    op.add_column(
        "request_price_selections",
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "request_price_selections",
        sa.Column("published_by", sa.BigInteger()),
    )
    op.add_column(
        "request_price_selections",
        sa.Column("publication_reason", sa.Text()),
    )
    op.add_column(
        "request_price_selections",
        sa.Column("telegram_outbox_id", sa.BigInteger()),
    )
    op.add_column(
        "request_price_selections",
        sa.Column("superseded_by_id", sa.BigInteger()),
    )
    op.create_foreign_key(
        "fk_request_price_selections_published_by",
        "request_price_selections",
        "employees",
        ["published_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_request_price_selections_outbox",
        "request_price_selections",
        "telegram_outbox",
        ["telegram_outbox_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_request_price_selections_superseded_by",
        "request_price_selections",
        "request_price_selections",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_request_price_selections_idempotency",
        "request_price_selections",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_request_price_selections_idempotency",
        table_name="request_price_selections",
    )
    op.drop_constraint(
        "fk_request_price_selections_superseded_by",
        "request_price_selections",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_request_price_selections_outbox",
        "request_price_selections",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_request_price_selections_published_by",
        "request_price_selections",
        type_="foreignkey",
    )
    op.drop_column("request_price_selections", "superseded_by_id")
    op.drop_column("request_price_selections", "telegram_outbox_id")
    op.drop_column("request_price_selections", "publication_reason")
    op.drop_column("request_price_selections", "published_by")
    op.drop_column("request_price_selections", "published_at")
    op.drop_column("request_price_selections", "idempotency_key")
    op.drop_column("quotes", "observed_at")
