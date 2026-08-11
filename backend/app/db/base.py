"""SQLAlchemy DeclarativeBase for Zakupki-Bot ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base. No shared models with tg-channel-parser."""
