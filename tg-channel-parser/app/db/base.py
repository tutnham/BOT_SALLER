"""SQLAlchemy DeclarativeBase for tg-channel-parser (isolated from zakupki)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base. No shared models with Zakupki-Bot."""
