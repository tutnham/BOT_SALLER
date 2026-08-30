"""SQLAlchemy models — TG_CHANNEL_PARSER_DOCUMENTATION.md §9 DDL."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ParserContentType(str, enum.Enum):
    text = "text"
    photo = "photo"
    video = "video"
    document = "document"
    audio = "audio"
    voice = "voice"
    media_group = "media_group"
    other = "other"


class ParserStatus(str, enum.Enum):
    new = "new"
    downloaded = "downloaded"
    processing = "processing"
    processed = "processed"
    failed = "failed"


class ParserTaskType(str, enum.Enum):
    download_media = "download_media"
    ai_process = "ai_process"
    resolve_channel = "resolve_channel"


class ParserChannelStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    failed = "failed"


class ParserMediaType(str, enum.Enum):
    photo = "photo"
    video = "video"
    document = "document"
    audio = "audio"
    voice = "voice"


class ParserStorageBackend(str, enum.Enum):
    local = "local"
    s3 = "s3"


class ParserChannelPurpose(str, enum.Enum):
    monitoring = "monitoring"
    supplier_price_source = "supplier_price_source"


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda members: [m.value for m in members],
    )


parser_content_type_enum = _pg_enum(ParserContentType, "parser_content_type")
parser_status_enum = _pg_enum(ParserStatus, "parser_status")
parser_task_type_enum = _pg_enum(ParserTaskType, "parser_task_type")
parser_channel_status_enum = _pg_enum(ParserChannelStatus, "parser_channel_status")
parser_media_type_enum = _pg_enum(ParserMediaType, "parser_media_type")
parser_storage_backend_enum = _pg_enum(ParserStorageBackend, "parser_storage_backend")
parser_channel_purpose_enum = _pg_enum(ParserChannelPurpose, "parser_channel_purpose")


class ParserChannel(Base):
    __tablename__ = "parser_channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    username: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[ParserChannelPurpose] = mapped_column(
        parser_channel_purpose_enum,
        nullable=False,
        server_default=sa_text("'monitoring'"),
    )
    status: Mapped[ParserChannelStatus] = mapped_column(
        parser_channel_status_enum,
        nullable=False,
        server_default=sa_text("'active'"),
    )
    error_text: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    last_synced_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    posts: Mapped[list[ParserPost]] = relationship(back_populates="channel")


class ParserPost(Base):
    __tablename__ = "parser_posts"
    __table_args__ = (UniqueConstraint("channel_id", "message_id", name="uq_parser_posts_channel_message"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("parser_channels.channel_id"),
        nullable=False,
    )
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    grouped_id: Mapped[int | None] = mapped_column(BigInteger)
    post_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_type: Mapped[ParserContentType] = mapped_column(
        parser_content_type_enum, nullable=False
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    message_link: Mapped[str | None] = mapped_column(Text)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[ParserStatus] = mapped_column(
        parser_status_enum, nullable=False, server_default=sa_text("'new'")
    )
    excluded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    excluded_reason: Mapped[str | None] = mapped_column(Text)
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_text: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    channel: Mapped[ParserChannel] = relationship(back_populates="posts")
    media_files: Mapped[list[ParserMediaFile]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[ParserTask]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    ai_results: Mapped[list[ParserAiResult]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class ParserMediaFile(Base):
    __tablename__ = "parser_media_files"
    __table_args__ = (
        UniqueConstraint("post_id", "file_unique_id", name="uq_parser_media_post_file"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("parser_posts.id", ondelete="CASCADE"), nullable=False
    )
    media_type: Mapped[ParserMediaType] = mapped_column(parser_media_type_enum, nullable=False)
    file_unique_id: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[str | None] = mapped_column(Text)
    storage_backend: Mapped[ParserStorageBackend] = mapped_column(
        parser_storage_backend_enum, nullable=False
    )
    storage_path: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(Text)
    sha256_hash: Mapped[str | None] = mapped_column(Text)
    download_status: Mapped[ParserStatus] = mapped_column(
        parser_status_enum, nullable=False, server_default=sa_text("'new'")
    )
    error_text: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    post: Mapped[ParserPost] = relationship(back_populates="media_files")


class ParserTask(Base):
    __tablename__ = "parser_tasks"
    __table_args__ = (
        Index("idx_parser_tasks_pickup", "status", "scheduled_at"),
        Index("idx_parser_tasks_resolve_channel", "status", "task_type", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("parser_posts.id", ondelete="CASCADE"), nullable=True
    )
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("parser_channels.id", ondelete="CASCADE"), nullable=True
    )
    task_type: Mapped[ParserTaskType] = mapped_column(parser_task_type_enum, nullable=False)
    status: Mapped[ParserStatus] = mapped_column(
        parser_status_enum, nullable=False, server_default=sa_text("'new'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("3"))
    error_text: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    post: Mapped[ParserPost] = relationship(back_populates="tasks")


class ParserAiResult(Base):
    """Forward-compat table; MVP does not write AI results."""

    __tablename__ = "parser_ai_results"
    __table_args__ = (Index("idx_parser_ai_results_post", "post_id", "is_current"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("parser_posts.id", ondelete="CASCADE"), nullable=False
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[Any | None] = mapped_column(JSONB)
    key_topics: Mapped[Any | None] = mapped_column(JSONB)
    toxicity_score: Mapped[float | None] = mapped_column(REAL)
    has_profanity: Mapped[bool | None] = mapped_column(Boolean)
    cleaned_text: Mapped[str | None] = mapped_column(Text)
    classification_reason: Mapped[str | None] = mapped_column(Text)
    schema_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_used: Mapped[str] = mapped_column(Text, nullable=False)
    raw_ai_response: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    post: Mapped[ParserPost] = relationship(back_populates="ai_results")
