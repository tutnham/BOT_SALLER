"""Question contract sent to POST /v1/systemone. Versions invalidate the cache."""

from __future__ import annotations

from typing import Any

from app.integrations.jev.models import JevCandidate

PROMPT_VERSION = "supplier-classify-v4"
SCHEMA_VERSION = "jev-decision-v1"
PROVIDER = "jev"

MESSAGE_CHOICES = ("price_offer", "not_price", "ambiguous")
BINDING_NONE = "none"
BINDING_AMBIGUOUS = "ambiguous"

_IS_PRICE_TRUE = (
    "A commercial supplier price: a full amount, a thousands suffix (к), "
    "or a short decimal on a product line."
)
_MATCHES_ORDER_TRUE = (
    "The message names the same product line, generation, and variant as at least one listed order."
)
_MATCHES_ORDER_FALSE = (
    "The message names a different product: another line, another generation, another variant, "
    "or a product that is not in `candidates`."
)
_IS_PRICE_FALSE = (
    "Not a commercial price: model numbers, storage size, quantity, request numbers, "
    "dates, phone numbers, message ids, greetings, advertisements, or availability-only text."
)


def build_state(message_text: str, candidates: list[JevCandidate]) -> dict[str, Any]:
    return {
        "supplier_message": message_text,
        "candidates": [item.model_dump(exclude_none=True) for item in candidates],
    }


def build_questions(candidates: list[JevCandidate]) -> dict[str, Any]:
    binding_criteria: dict[str, str] = {
        BINDING_NONE: (
            "The message names a product that is not any listed order, "
            "or it does not refer to a listed order."
        ),
        BINDING_AMBIGUOUS: "More than one listed candidate fits, or the text is only a bare price.",
    }
    for candidate in candidates:
        binding_criteria[candidate.candidate_id] = candidate.description[:240]
    return {
        "message_type": {
            "type": "choice",
            "instructions": (
                "Classify the supplier message. price_offer is a commercial price. "
                "not_price is greeting, ad, availability, model, storage, qty, date, or phone. "
                "ambiguous when it is unclear."
            ),
            "criteria": {
                "price_offer": "A commercial price, whether or not it matches a listed order.",
                "not_price": "No commercial price.",
                "ambiguous": "Could be either.",
            },
        },
        "matches_listed_order": {
            "type": "noul",
            "instructions": (
                "Does `supplier_message` name the same product model as any order in `candidates`?"
            ),
            "criteria": {"true": _MATCHES_ORDER_TRUE, "false": _MATCHES_ORDER_FALSE},
        },
        "is_price": {
            "type": "noul",
            "instructions": "Does this supplier message contain a commercial price?",
            "criteria": {"true": _IS_PRICE_TRUE, "false": _IS_PRICE_FALSE},
        },
        "binding": {
            "type": "choice",
            "instructions": (
                "Which order in `candidates` is `supplier_message` answering? "
                "Match the message to that order's order_text and attributes. "
                "outgoing_at is when that order was sent to this supplier. "
                "Choose only from the criteria keys. "
                "A bare price with several candidates is ambiguous. "
                "Do not invent an identifier."
            ),
            "criteria": binding_criteria,
        },
    }
