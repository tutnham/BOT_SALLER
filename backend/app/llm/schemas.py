"""Pydantic contracts for LLM structured outputs (TECH DOC §8)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class NormalizedRequest(BaseModel):
    """Schema from TECH DOC §8.1."""

    model: str
    storage: str | None = None
    color: str | None = None
    region: str | None = None
    sim: str | None = None
    qty: int | None = None
    condition: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class BatchNormalizedItem(BaseModel):
    """Structured LLM item for one batch source line. Money fields forbidden."""

    source_line_no: int
    source_text: str
    brand: str | None = None
    family: str | None = None
    model: str = ""
    variant: str | None = None
    storage_gb: int | None = None
    color: str | None = None
    sim: str | None = None
    region: str | None = None
    quantity: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguities: list[str] = Field(default_factory=list)


class ProductClassification(BaseModel):
    category: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class SupplierReplyBinding(BaseModel):
    related: bool = False
    request_id: int | None = None
    price: Decimal | None = None
    currency: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class ParsedSupplierReply(BaseModel):
    """Schema from TECH DOC §8.3."""

    available: bool | None = None
    qty: int | None = None
    price: Decimal | None = None
    min_sale_price: Decimal | None = None
    condition: str | None = None
    note: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class ParsedPriceItem(BaseModel):
    """Single SKU row from TECH DOC §8.2."""

    model: str
    storage: str | None = None
    color: str | None = None
    region: str | None = None
    sim: str | None = None
    condition: str | None = None
    price: Decimal = Field(gt=0)
    currency: str = "RUB"
    confidence: float = Field(ge=0.0, le=1.0)


class ParsedPriceList(BaseModel):
    """Schema from TECH DOC §8.2."""

    items: list[ParsedPriceItem] = Field(default_factory=list)


def validate_product_classification(payload: Any) -> ProductClassification:
    if not isinstance(payload, dict):
        return ProductClassification(category="unknown", confidence=0.0)
    try:
        return ProductClassification.model_validate(payload)
    except ValidationError:
        return ProductClassification(category="unknown", confidence=0.0)


def validate_supplier_reply_binding(payload: Any) -> SupplierReplyBinding:
    if not isinstance(payload, dict):
        return SupplierReplyBinding(related=False, confidence=0.0)
    try:
        return SupplierReplyBinding.model_validate(payload)
    except ValidationError:
        return SupplierReplyBinding(related=False, confidence=0.0)


def validate_price_list_payload(payload: Any) -> ParsedPriceList:
    """
    Validate provider payload item-by-item.

    Invalid envelope → empty list. Invalid items are skipped.
    """
    if not isinstance(payload, dict):
        return ParsedPriceList(items=[])
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return ParsedPriceList(items=[])

    valid: list[ParsedPriceItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            valid.append(ParsedPriceItem.model_validate(item))
        except ValidationError:
            continue
    return ParsedPriceList(items=valid)


class TopSupplierMetric(BaseModel):
    name: str
    deals_count: int


class ReportMetrics(BaseModel):
    """Input contract for report formatting (§8.4, §9.6)."""

    period: str
    period_key: str
    requests_total: int
    deals_closed: int
    messages_out_total: int
    messages_in_total: int
    avg_price_initial: Decimal | None = None
    avg_final_price: Decimal | None = None
    savings_vs_initial: Decimal | None = None
    top_suppliers: list[TopSupplierMetric] = Field(default_factory=list)
    open_requests: int
