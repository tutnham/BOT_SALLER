"""Partial unique indexes for Telegram message identity per Business route.

Revision ID: 0017_route_identity_partial_unique
Revises: 0016_admin_audit_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_route_identity_partial_unique"
down_revision: str | None = "0016_admin_audit_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUPLICATE_SQL = """
SELECT chat_id, tg_message_id, COALESCE(business_connection_id, '') AS bc_key, COUNT(*) AS cnt
FROM {table}
GROUP BY chat_id, tg_message_id, COALESCE(business_connection_id, '')
HAVING COUNT(*) > 1
LIMIT 20
"""

_LEGACY_DUPLICATE_SQL = """
SELECT chat_id, tg_message_id, COUNT(*) AS cnt
FROM {table}
GROUP BY chat_id, tg_message_id
HAVING COUNT(*) > 1
LIMIT 20
"""


def _fail_if_duplicates(conn: sa.Connection, table: str) -> None:
    rows = conn.execute(sa.text(_DUPLICATE_SQL.format(table=table))).fetchall()
    if rows:
        sample = ", ".join(
            f"(chat_id={r[0]}, msg_id={r[1]}, bc={r[2]!r}, count={r[3]})" for r in rows[:5]
        )
        raise RuntimeError(
            f"Cannot apply route-identity indexes on {table}: duplicate message keys exist. "
            f"Resolve duplicates first. Sample: {sample}"
        )


def _fail_if_legacy_duplicates(conn: sa.Connection, table: str) -> None:
    rows = conn.execute(sa.text(_LEGACY_DUPLICATE_SQL.format(table=table))).fetchall()
    if rows:
        sample = ", ".join(f"(chat_id={r[0]}, msg_id={r[1]}, count={r[2]})" for r in rows[:5])
        raise RuntimeError(
            f"Cannot restore global UNIQUE on {table}: rows share chat_id+tg_message_id "
            f"across routes. Sample: {sample}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    _fail_if_duplicates(conn, "messages_in")
    _fail_if_duplicates(conn, "messages_out")

    for table in ("messages_in", "messages_out"):
        op.drop_constraint(f"{table}_chat_id_tg_message_id_key", table, type_="unique")

    op.create_index(
        "uq_messages_in_bot_route",
        "messages_in",
        ["chat_id", "tg_message_id"],
        unique=True,
        postgresql_where=sa.text("business_connection_id IS NULL"),
    )
    op.create_index(
        "uq_messages_in_business_route",
        "messages_in",
        ["business_connection_id", "chat_id", "tg_message_id"],
        unique=True,
        postgresql_where=sa.text("business_connection_id IS NOT NULL"),
    )
    op.create_index(
        "uq_messages_out_bot_route",
        "messages_out",
        ["chat_id", "tg_message_id"],
        unique=True,
        postgresql_where=sa.text("business_connection_id IS NULL"),
    )
    op.create_index(
        "uq_messages_out_business_route",
        "messages_out",
        ["business_connection_id", "chat_id", "tg_message_id"],
        unique=True,
        postgresql_where=sa.text("business_connection_id IS NOT NULL"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    _fail_if_legacy_duplicates(conn, "messages_in")
    _fail_if_legacy_duplicates(conn, "messages_out")

    op.drop_index("uq_messages_in_business_route", table_name="messages_in")
    op.drop_index("uq_messages_in_bot_route", table_name="messages_in")
    op.drop_index("uq_messages_out_business_route", table_name="messages_out")
    op.drop_index("uq_messages_out_bot_route", table_name="messages_out")

    op.create_unique_constraint(
        "messages_in_chat_id_tg_message_id_key",
        "messages_in",
        ["chat_id", "tg_message_id"],
    )
    op.create_unique_constraint(
        "messages_out_chat_id_tg_message_id_key",
        "messages_out",
        ["chat_id", "tg_message_id"],
    )
