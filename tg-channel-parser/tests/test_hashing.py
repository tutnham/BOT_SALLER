"""Tests for chunked file hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.utils.hashing import sha256_file


def test_sha256_file_matches_full_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.bin"
    payload = b"chunked" * 10000
    target.write_bytes(payload)

    expected = hashlib.sha256(payload).hexdigest()
    assert sha256_file(target) == expected
