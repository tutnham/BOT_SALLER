"""SQLAlchemy 2.x models — one-to-one with TECHNICAL_DOCUMENTATION.md §6 DDL."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# --- PostgreSQL ENUMs (native) -------------------------------------------------


class RequestStatus(str, enum.Enum):
    open = "open"
    awaiting_answers = "awaiting_answers"
    priced = "priced"
    bargaining = "bargaining"
    needs_recheck = "needs_recheck"
    closed = "closed"
    cancelled = "cancelled"


class QuoteSource(str, enum.Enum):
    regex = "regex"
    llm = "llm"
    manual = "manual"


class DealOutcome(str, enum.Enum):
    won = "won"
    lost = "lost"
    cancelled = "cancelled"


class MessageKind(str, enum.Enum):
    ask = "ask"
    bargain = "bargain"
    recheck = "recheck"


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Native PG ENUM using enum *values* (lowercase) to match DDL §6."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda members: [m.value for m in members],
    )


request_status_enum = _pg_enum(RequestStatus, "request_status")
quote_source_enum = _pg_enum(QuoteSource, "quote_source")
deal_outcome_enum = _pg_enum(DealOutcome, "deal_outcome")
message_kind_enum = _pg_enum(MessageKind, "message_kind")


# --- Tables --------------------------------------------------------------------


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    dm_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    requests: Mapped[list[Request]] = relationship(back_populates="employee")


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    dm_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    price_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    price_channel_username: Mapped[str | None] = mapped_column(Text)
    last_price_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ClientGroup(Base):
    __tablename__ = "client_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )


class Request(Base):
    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    employee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=False
    )
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[RequestStatus] = mapped_column(
        request_status_enum,
        nullable=False,
        server_default=sa_text("'open'::request_status"),
    )
    recheck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    employee: Mapped[Employee] = relationship(back_populates="requests")
    messages_out: Mapped[list[MessageOut]] = relationship(back_populates="request")
    messages_in: Mapped[list[MessageIn]] = relationship(back_populates="request")
    quotes: Mapped[list[Quote]] = relationship(back_populates="request")
    deals: Mapped[list[Deal]] = relationship(back_populates="request")


class MessageOut(Base):
    __tablename__ = "messages_out"
    __table_args__ = (UniqueConstraint("chat_id", "tg_message_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("requests.id")
    )
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[MessageKind] = mapped_column(message_kind_enum, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    request: Mapped[Request | None] = relationship(back_populates="messages_out")
    supplier: Mapped[Supplier] = relationship()


class MessageIn(Base):
    __tablename__ = "messages_in"
    __table_args__ = (UniqueConstraint("chat_id", "tg_message_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("requests.id")
    )
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    request: Mapped[Request | None] = relationship(back_populates="messages_in")
    supplier: Mapped[Supplier] = relationship()


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("request_id", "supplier_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("requests.id"), nullable=False
    )
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    price_initial: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_bargain: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    qty: Mapped[int | None] = mapped_column(Integer)
    available: Mapped[bool | None] = mapped_column(Boolean)
    condition: Mapped[str | None] = mapped_column(Text)
    source: Mapped[QuoteSource] = mapped_column(quote_source_enum, nullable=False)
    confidence: Mapped[float | None] = mapped_column(REAL)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    request: Mapped[Request] = relationship(back_populates="quotes")
    supplier: Mapped[Supplier] = relationship()


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("requests.id"), nullable=False
    )
    chosen_supplier_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("suppliers.id")
    )
    final_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    outcome: Mapped[DealOutcome | None] = mapped_column(deal_outcome_enum)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    request: Mapped[Request] = relationship(back_populates="deals")
    chosen_supplier: Mapped[Supplier | None] = relationship()


class RawPrice(Base):
    __tablename__ = "raw_prices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'manual_message'")
    )
    source_post_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_link: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    supplier: Mapped[Supplier] = relationship()
    parsed_items: Mapped[list[ParsedItem]] = relationship(back_populates="raw_price")


class ParsedItem(Base):
    __tablename__ = "parsed_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_price_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_prices.id"), nullable=False
    )
    sku_key: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    storage: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    sim: Mapped[str | None] = mapped_column(Text)
    condition: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'RUB'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    raw_price: Mapped[RawPrice] = relationship(back_populates="parsed_items")


class MarkupRule(Base):
    __tablename__ = "markup_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    markup_fixed: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    markup_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    markup_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )


class PriceListDraft(Base):
    __tablename__ = "price_list_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    items: Mapped[dict[str, Any] | list[Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'pending'")
    )
    approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("employees.id")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParseCache(Base):
    __tablename__ = "parse_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    content_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_used: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReportCache(Base):
    __tablename__ = "report_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    formatted_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UpdateLog(Base):
    __tablename__ = "update_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_update_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
