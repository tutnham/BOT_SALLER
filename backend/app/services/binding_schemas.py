"""Structured binding and price parse results (RFQ supplier replies)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.parsers.price_parse import PriceParseResult

BindingStatus = Literal["bound", "pending", "unbound", "ignored"]
BindingMethod = Literal[
    "reply",
    "explicit_id",
    "single_bare_price",
    "product_match",
    "llm",
    "employee",
    "none",
]


@dataclass(frozen=True)
class BindingResult:
    request_id: int | None
    status: BindingStatus
    method: BindingMethod
    confidence: float | None
    candidate_ids: list[int] = field(default_factory=list)
    conflict_reason: str | None = None


@dataclass(frozen=True)
class SupplierReplyResolution:
    """Combined binding + price for one inbound message or line."""

    binding: BindingResult
    price: PriceParseResult
    binding_price: Decimal | None = None
