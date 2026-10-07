"""Alembic must have a single head; parent-to-head upgrade preserves data."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "app" / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_single_alembic_head() -> None:
    heads = _script_dir().get_heads()
    assert len(heads) == 1


def test_parent_revision_is_defined() -> None:
    script = _script_dir()
    head = script.get_current_head()
    assert head is not None
    revision = script.get_revision(head)
    assert revision is not None
    assert revision.down_revision is not None


def test_head_is_deal_purchase_audit() -> None:
    from app.db.revision import EXPECTED_ALEMBIC_REVISION

    head = _script_dir().get_current_head()
    assert head == "0026_deal_purchase_audit"
    assert EXPECTED_ALEMBIC_REVISION == head


def test_batch_revisions_follow_0021() -> None:
    script = _script_dir()
    rev = script.get_revision("0026_deal_purchase_audit")
    chain: list[str] = []
    while rev is not None:
        chain.append(rev.revision)
        if rev.revision == "0021_iphone_18_markup_rules":
            break
        down = rev.down_revision
        assert isinstance(down, str)
        rev = script.get_revision(down)
    assert chain == [
        "0026_deal_purchase_audit",
        "0025_request_price_selections",
        "0024_daily_sku_prices",
        "0023_supplier_rfq_groups",
        "0022_request_batches",
        "0021_iphone_18_markup_rules",
    ]
