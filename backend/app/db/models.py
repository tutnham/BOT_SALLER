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
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
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


class MessageSendStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class WebhookInboxStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    dead = "dead"


class TelegramOutboxStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    sent = "sent"
    failed = "failed"
    dead = "dead"
    uncertain = "uncertain"


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


class ProductCategory(str, enum.Enum):
    apple = "apple"
    samsung = "samsung"
    power_station = "power_station"
    other = "other"


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    rfq_enabled: Mapped[bool] = mapped_column(
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

    chats: Mapped[list[SupplierChat]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
    )
    categories: Mapped[list[SupplierCategory]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
    )


class SupplierCategory(Base):
    __tablename__ = "supplier_categories"
    __table_args__ = (
        CheckConstraint(
            "category IN ('apple', 'samsung', 'power_station', 'other')",
            name="ck_supplier_categories_category",
        ),
    )

    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[str] = mapped_column(Text, primary_key=True)

    supplier: Mapped[Supplier] = relationship(back_populates="categories")


class BusinessConnection(Base):
    """Telegram Business bot connection to a client's Premium account."""

    __tablename__ = "business_connections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    business_connection_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    can_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    can_read_messages: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    raw_rights: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SupplierChatType(str, enum.Enum):
    private = "private"
    group = "group"
    supergroup = "supergroup"
    business_dm = "business_dm"


class SupplierChat(Base):
    __tablename__ = "supplier_chats"
    __table_args__ = (
        UniqueConstraint("chat_id", "chat_type", name="uq_supplier_chats_chat_id_type"),
        Index(
            "ix_supplier_chats_default",
            "supplier_id",
            unique=True,
            postgresql_where=sa_text("is_default = true"),
        ),
        Index("ix_supplier_chats_business_connection_id", "business_connection_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_type: Mapped[SupplierChatType] = mapped_column(
        _pg_enum(SupplierChatType, "supplier_chat_type"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    bound_by_owner_id: Mapped[int | None] = mapped_column(BigInteger)
    business_connection_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    supplier: Mapped[Supplier] = relationship(back_populates="chats")


class PendingChat(Base):
    __tablename__ = "pending_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_type: Mapped[SupplierChatType] = mapped_column(
        _pg_enum(SupplierChatType, "supplier_chat_type"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(Text)
    invited_by_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminDialog(Base):
    __tablename__ = "admin_dialogs"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now() + interval '15 minutes'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupplierBindToken(Base):
    __tablename__ = "supplier_bind_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    created_by_owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    bound_by_owner_id: Mapped[int | None] = mapped_column(BigInteger)


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (Index("ix_requests_employee_id", "employee_id"),)

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
    __table_args__ = (
        UniqueConstraint("chat_id", "tg_message_id"),
        CheckConstraint(
            "send_status IN ('pending', 'sent', 'failed')",
            name="ck_messages_out_send_status",
        ),
        Index("ix_messages_out_request_id", "request_id"),
        Index("ix_messages_out_supplier_request", "supplier_id", "request_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("requests.id")
    )
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[MessageKind] = mapped_column(message_kind_enum, nullable=False)
    business_connection_id: Mapped[str | None] = mapped_column(Text)
    send_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=sa_text("'sent'"),
    )
    error_text: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    request: Mapped[Request | None] = relationship(back_populates="messages_out")
    supplier: Mapped[Supplier] = relationship()


class MessageIn(Base):
    __tablename__ = "messages_in"
    __table_args__ = (
        UniqueConstraint("chat_id", "tg_message_id"),
        Index("ix_messages_in_request_id", "request_id"),
        Index("ix_messages_in_supplier_id", "supplier_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("requests.id")
    )
    supplier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("suppliers.id"), nullable=False
    )
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_connection_id: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    bind_method: Mapped[str | None] = mapped_column(Text)
    bind_status: Mapped[str | None] = mapped_column(Text)
    bind_score: Mapped[float | None] = mapped_column(REAL)
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
    markup_rub: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_final: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    markup_rule_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("markup_rules.id", ondelete="SET NULL")
    )
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
    markup_rule: Mapped[MarkupRule | None] = relationship()


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_deals_request_id"),
        Index("ix_deals_request_id", "request_id"),
        Index("ix_deals_chosen_supplier_id", "chosen_supplier_id"),
    )

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
    __table_args__ = (Index("ix_raw_prices_supplier_id", "supplier_id"),)

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
    __table_args__ = (Index("ix_parsed_items_raw_price_id", "raw_price_id"),)

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
    __table_args__ = (
        UniqueConstraint("rule_key", name="uq_markup_rules_rule_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    markup_fixed: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    markup_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    markup_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    rule_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PriceListDraft(Base):
    __tablename__ = "price_list_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # list[dict], not dict|list: SQLAlchemy 2 + py3.11 ForwardRef crash (Coolify image)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'pending'")
    )
    approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("employees.id")
    )
    approved_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
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


class WebhookInbox(Base):
    __tablename__ = "webhook_inbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'dead')",
            name="ck_webhook_inbox_status",
        ),
        Index("ix_webhook_inbox_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_update_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramOutbox(Base):
    __tablename__ = "telegram_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', 'dead', 'uncertain')",
            name="ck_telegram_outbox_status",
        ),
        Index("ix_telegram_outbox_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger)
    request_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    business_connection_id: Mapped[str | None] = mapped_column(Text)
    parse_mode: Mapped[str | None] = mapped_column(Text)
    reply_markup: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    message_out_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages_out.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpdateLog(Base):
    __tablename__ = "update_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_update_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AppSetting(Base):
    """Runtime-mutable configuration keyed by a lower_snake_case string."""

    __tablename__ = "app_settings"
    __table_args__ = (Index("ix_app_settings_updated_by", "updated_by"),)

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("owners.id"), nullable=True
    )


class BillingReminder(Base):
    """Idempotency log for periodic billing reminders by (kind, period_key)."""

    __tablename__ = "billing_reminders"
    __table_args__ = (
        Index(
            "ix_billing_reminders_kind_period_key",
            "kind",
            "period_key",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    period_key: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    chat_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
