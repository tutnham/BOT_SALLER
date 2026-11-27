"""price_list_drafts.review_sources: происхождение цены для проверки владельцем.

items остаются публично-безопасными (acceptance-тест запрещает закуп/наценку
внутри), поэтому канал, закуп и альтернативная цена живут отдельным полем и
уходят владельцу вторым сообщением, а не в прайс.

Revision ID: 0033_price_draft_review_sources
Revises: 0032_parsed_sku_key_slugs
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0033_price_draft_review_sources"
down_revision: str | None = "0032_parsed_sku_key_slugs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_list_drafts",
        sa.Column("review_sources", JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("price_list_drafts", "review_sources")
