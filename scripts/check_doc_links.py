#!/usr/bin/env python3
"""Verify relative markdown links in canonical docs resolve to existing files."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = [
    ROOT / "README.md",
    ROOT / "CLAUDE.md",
    ROOT / "AGENTS.md",
    ROOT / "СЕРВИСЫ.md",
    ROOT / "PHASE4_SETUP.md",
    ROOT / "TG_CHANNEL_PARSER_DOCUMENTATION.md",
    ROOT / "Техническая документация  бот для закупок.md",
    ROOT / "TELEGRAM_BUSINESS_SETUP.md",
    ROOT / "docs" / "archive" / "README.md",
    ROOT / "docs" / "BRANCH_PROTECTION.md",
]

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing canonical doc: {path.relative_to(ROOT)}"]
    text = path.read_text(encoding="utf-8")
    for match in LINK_RE.finditer(text):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0].strip()
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.is_file():
            errors.append(
                f"{path.relative_to(ROOT)}: broken link {target!r}"
            )
    return errors


def main() -> int:
    errors: list[str] = []
    for doc in CANONICAL:
        errors.extend(check_file(doc))
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    print(f"OK: checked {len(CANONICAL)} docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
