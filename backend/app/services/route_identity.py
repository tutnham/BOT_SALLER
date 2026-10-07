"""Telegram delivery route identity for supplier binding."""

from __future__ import annotations


def normalize_business_connection_id(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def routes_match(
    left_business_connection_id: str | None,
    right_business_connection_id: str | None,
) -> bool:
    return normalize_business_connection_id(left_business_connection_id) == normalize_business_connection_id(
        right_business_connection_id
    )


def route_type_label(business_connection_id: str | None) -> str:
    return "business" if normalize_business_connection_id(business_connection_id) else "bot"
