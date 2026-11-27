"""price_publish_channels: реестр каналов, куда кладётся утверждённый прайс.

Цель публикации раньше жила только в env (PRICE_PUBLISH_CHAT_IDS), и добавить
канал можно было только через перезапуск контейнера. Таблица делает список
данными: экран в меню владельца, вкл/выкл без деплоя. Env остаётся fallback на
один релиз (строки с source='env' подсеваются при первом прогоне).

Revision ID: 0035_price_publish_channels
Revises: 0034_channel_sku_prices
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_price_publish_channels"
down_revision: str | None = "0034_channel_sku_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_publish_channels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", name="uq_price_publish_channels_chat_id"),
    )
    op.create_index(
        "ix_price_publish_channels_active", "price_publish_channels", ["active"]
    )


def downgrade() -> None:
    op.drop_index("ix_price_publish_channels_active", table_name="price_publish_channels")
    op.drop_table("price_publish_channels")
