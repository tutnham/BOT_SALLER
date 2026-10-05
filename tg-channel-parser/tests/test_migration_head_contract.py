"""Parser Alembic tree must remain linear."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PARSER_ROOT = Path(__file__).resolve().parents[1]


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(PARSER_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PARSER_ROOT / "app" / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_single_alembic_head() -> None:
    assert len(_script_dir().get_heads()) == 1
