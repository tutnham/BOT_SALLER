"""Shared product normalizer tests."""

from __future__ import annotations

from app.parsers.product_normalizer import (
    canonicalize_catalog_fields,
    extract_product_attrs,
    normalize_product_text,
)


def test_normalize_cyrillic_and_glued_tokens() -> None:
    assert "iphone 17 pro max" in normalize_product_text("айфон 17промакс")
    assert "iphone 16 pro" in normalize_product_text("айфон 16 про")


def test_extract_attrs_16e() -> None:
    attrs = extract_product_attrs("iPhone 16e 128 black")
    assert attrs.number == "16"
    assert attrs.variant == "e"


def test_brandless_18_pro_is_iphone() -> None:
    attrs = extract_product_attrs("18 pro 512gb")
    assert attrs.brand == "iPhone"
    assert attrs.model == "iPhone 18 Pro"
    assert attrs.storage == "512GB"
    assert attrs.variant == "pro"


def test_brandless_14_storage_is_iphone() -> None:
    attrs = extract_product_attrs("14 128GB Yellow")
    assert attrs.model == "iPhone 14"
    assert attrs.variant == "base"


def test_iphone_duo_model() -> None:
    attrs = extract_product_attrs("iphone duo 256gb")
    assert attrs.model == "iPhone Duo"
    assert attrs.variant == "duo"


def test_canonicalize_keeps_non_iphone_names() -> None:
    assert canonicalize_catalog_fields("Samsung Galaxy S24 Ultra 256GB") == (
        "Samsung Galaxy S24 Ultra 256GB",
        None,
    )
    assert canonicalize_catalog_fields("MacBook Air 13 M2 8/256") == (
        "MacBook Air 13 M2 8/256",
        None,
    )
    assert canonicalize_catalog_fields("Apple Watch Ultra 2 49mm") == (
        "Apple Watch Ultra 2 49mm",
        None,
    )


def test_duo_sim_keeps_model_number() -> None:
    attrs = extract_product_attrs("iPhone 15 duo sim 512gb")
    assert attrs.model == "iPhone 15"
    assert attrs.variant == "base"
    assert attrs.sim == "Dual SIM"


def test_duo_model_ignores_duo_sim() -> None:
    attrs = extract_product_attrs("iphone duo sim 256gb")
    assert attrs.variant != "duo"


def test_brandless_air_13_is_not_iphone() -> None:
    attrs = extract_product_attrs("Air 13 256GB")
    assert attrs.brand is None
    assert attrs.number == "13"
    assert attrs.variant == "air"


def test_brandless_air_17_is_iphone_air() -> None:
    attrs = extract_product_attrs("air 17 256gb")
    assert attrs.model == "iPhone 17 Air"


def test_canonicalize_keeps_air_13_line() -> None:
    assert canonicalize_catalog_fields("Air 13", "256GB") == ("Air 13", "256GB")
