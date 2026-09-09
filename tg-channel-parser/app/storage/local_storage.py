"""Local filesystem media storage."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from app.config import get_settings


class LocalStorage:
    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_settings().media_local_path)

    async def save(self, local_tmp_path: str, key: str) -> str:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, local_tmp_path, dest)
        return str(dest)


def media_key(channel_id: int, message_id: int, file_unique_id: str, ext: str) -> str:
    safe_ext = ext.lstrip(".") if ext else "bin"
    return f"{channel_id}/{message_id}/{file_unique_id}.{safe_ext}"


def guess_ext(path: str, mime_type: str | None = None) -> str:
    _, ext = os.path.splitext(path)
    if ext:
        return ext.lstrip(".")
    if mime_type:
        mapping = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "video/mp4": "mp4",
            "application/pdf": "pdf",
        }
        return mapping.get(mime_type, "bin")
    return "bin"
