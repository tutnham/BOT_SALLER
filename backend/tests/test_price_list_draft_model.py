"""PriceListDraft JSONB typing — py3.11 SQLAlchemy ForwardRef footgun."""

from __future__ import annotations

from app.db.models import PriceListDraft


def test_price_list_draft_items_annotation_is_list_of_dict() -> None:
    """dict[str, Any] | list[Any] crashes SQLAlchemy 2 on Python 3.11."""
    ann = str(PriceListDraft.__annotations__["items"])
    assert "dict[str, Any] | list" not in ann
    assert "list[dict[str, Any]]" in ann


def test_price_list_draft_mapper_configures() -> None:
    assert PriceListDraft.__tablename__ == "price_list_drafts"
    assert "items" in PriceListDraft.__mapper__.columns
