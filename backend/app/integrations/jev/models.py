"""Vendor-neutral Jev schemas. Probabilities are Decimal in domain code."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class JevMessageClass(StrEnum):
    PRICE_OFFER = "price_offer"
    NOT_PRICE = "not_price"
    AMBIGUOUS = "ambiguous"


class JevDecisionAction(StrEnum):
    AUTO_BIND = "auto_bind"
    REVIEW = "review"
    IGNORE = "ignore"
    FALLBACK = "fallback"


class JevCandidate(BaseModel):
    """Minimum product context. No prices, identities, or secrets."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    description: str
    model: str | None = None
    storage: str | None = None
    color: str | None = None
    sim: str | None = None
    region: str | None = None
    quantity: int | None = None
    rfq_kind: str | None = None
    outgoing_at: str | None = None


class JevProviderResponse(BaseModel):
    """Parsed provider answers. The adapter does not choose AUTO_BIND."""

    model_config = ConfigDict(extra="forbid")

    model: str
    message_choice: str
    price_probability: Decimal = Field(ge=0, le=1)
    binding_choice: str
    binding_probabilities: dict[str, Decimal]
    binding_confidence: Decimal = Field(ge=0, le=1)
    input_tokens: int | None = None
    output_tokens: int | None = None


class JevSupplierDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_class: JevMessageClass
    price_probability: Decimal = Field(ge=0, le=1)

    selected_candidate_id: int | None = None
    selected_probability: Decimal | None = Field(default=None, ge=0, le=1)
    second_probability: Decimal | None = Field(default=None, ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)

    action: JevDecisionAction
    reason_code: str
    model: str
    schema_version: str

    input_hash: str = ""
    candidate_set_hash: str = ""
    latency_ms: int | None = None
    error_code: str | None = None
    from_cache: bool = False
    prompt_version: str = ""
    provider_model: str = ""
