"""MediaStorage protocol (doc §8.1)."""

from __future__ import annotations

from typing import Protocol


class MediaStorage(Protocol):
    async def save(self, local_tmp_path: str, key: str) -> str:
        """Persist file; return final storage_path / URL."""
        ...
