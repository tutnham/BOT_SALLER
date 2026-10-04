"""Settings guards for media storage."""

from __future__ import annotations

import pytest

from app.config import Settings


def test_require_media_storage_rejects_s3() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        api_auth_token="token",
        media_storage_backend="s3",
    )
    with pytest.raises(RuntimeError, match="not supported"):
        settings.require_media_storage()
