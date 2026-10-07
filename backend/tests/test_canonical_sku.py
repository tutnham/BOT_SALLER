from app.parsers.canonical_sku import build_canonical_sku_key, parse_batch_line


def test_iphone_14_yellow_key() -> None:
    attrs = parse_batch_line("14 128GB Yellow")
    key = build_canonical_sku_key(attrs)
    assert key == "v1|apple|iphone|14|128|yellow|*|*"


def test_17_air_black_key() -> None:
    attrs = parse_batch_line("17 Air 256GB Black")
    key = build_canonical_sku_key(attrs)
    assert key == "v1|apple|iphone|17-air|256|black|*|*"


def test_16_variants_differ() -> None:
    keys = {
        build_canonical_sku_key(parse_batch_line(line))
        for line in ("16 512GB Pink", "16e 128GB Black", "16 Plus 128GB Black", "16 Pro Max 256GB Desert")
    }
    assert len(keys) == 4


def test_storage_1tb_key_uses_1024() -> None:
    attrs = parse_batch_line("15 Pro 1TB Natural")
    key = build_canonical_sku_key(attrs)
    assert key is not None
    assert "|1024|" in key


def test_storage_variants_normalize() -> None:
    keys = [
        build_canonical_sku_key(parse_batch_line(line))
        for line in (
            "14 128GB Yellow",
            "14 256GB Yellow",
            "14 512GB Midnight",
            "15 Pro 1TB Natural",
        )
    ]
    storages = {key.split("|")[4] for key in keys if key}
    assert storages == {"128", "256", "512", "1024"}


def test_empty_lines_ignored_by_splitter() -> None:
    from app.parsers.batch_lines import split_batch_source_text

    lines = split_batch_source_text("14 128GB Yellow\n\n14 256GB Yellow\n")
    assert [item.source_text for item in lines] == ["14 128GB Yellow", "14 256GB Yellow"]


def test_duplicates_kept() -> None:
    from app.parsers.batch_lines import split_batch_source_text

    lines = split_batch_source_text("14 128GB Yellow\n14 128GB Yellow")
    assert len(lines) == 2
