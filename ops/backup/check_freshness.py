#!/usr/bin/env python3
"""Exit 1 when backup status JSON is missing or older than max age."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    path = Path(os.environ.get("BACKUP_STATUS_FILE", "last_backup.json"))
    max_age = int(os.environ.get("BACKUP_MAX_AGE_SECONDS", "93600"))
    if not path.is_file():
        print("missing status file", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    completed = data.get("completed_at")
    if not completed:
        return 1
    ts = datetime.strptime(completed, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    if age > max_age:
        print(f"backup stale age_seconds={int(age)}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "age_seconds": int(age)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
