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
