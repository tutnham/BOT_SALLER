"""Мост parsed_items → canonical_sku_key: чистая функция, без БД."""

from __future__ import annotations

from app.parsers.canonical_sku import (
    bridge_parsed_to_canonical,
    build_canonical_sku_key,
    parse_batch_line,
)


def bridge(**kwargs) -> str | None:
    return bridge_parsed_to_canonical(**kwargs)


def test_iphone_line_becomes_request_shaped_key() -> None:
    assert (
        bridge(model="iPhone 18 Pro Max", storage="512GB")
        == "v1|apple|iphone|18-pro-max|512|*|*|*"
    )


def test_bare_storage_without_unit_still_counts() -> None:
    assert bridge(model="iPhone 18 Pro Max", storage="512") == bridge(
        model="iPhone 18 Pro Max", storage="512GB"
    )


def test_storage_may_live_inside_model_text() -> None:
    assert (
        bridge(model="Apple iPhone 17 Pro 256GB", storage=None)
        == "v1|apple|iphone|17-pro|256|*|*|*"
    )


def test_key_matches_the_one_a_request_line_produces() -> None:
    """Пространства сходятся by construction: та же модель, тот же ключ."""
    from_channel = bridge(model="iPhone 17 Pro", storage="256GB", color="Natural Titanium")
    from_request = build_canonical_sku_key(parse_batch_line("17 pro 256GB natural titanium"))
    assert from_channel == from_request


def test_color_from_channel_row_narrows_the_key() -> None:
    colored = bridge(model="iPhone 15", storage="256GB", color="Black")
    plain = bridge(model="iPhone 15", storage="256GB")
    assert colored == "v1|apple|iphone|15|256|black|*|*"
    assert plain == "v1|apple|iphone|15|256|*|*|*"
    assert colored != plain


def test_region_and_sim_are_kept_as_given() -> None:
    assert (
        bridge(model="iPhone 15", storage="128GB", region="AE", sim="eSIM")
        == "v1|apple|iphone|15|128|*|esim|ae"
    )


def test_non_iphone_is_not_guessed() -> None:
    assert bridge(model="Galaxy S25 Ultra", storage="512GB") is None
    assert bridge(model="iPad Air 11 128GB", storage=None) is None
    assert bridge(model="MacBook Air 13 M4", storage="512GB") is None
    assert bridge(model=None, storage="512GB") is None
    assert bridge(model="", storage="") is None


def test_iphone_without_model_number_is_not_guessed() -> None:
    assert bridge(model="iPhone", storage="128GB") is None
    assert bridge(model="iPhone SE 4", storage="128GB") is None
