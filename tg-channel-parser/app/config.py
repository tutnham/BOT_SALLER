"""Settings from .env — secrets never logged."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config. Fail-fast on required secrets when processes need them."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram MTProto
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_phone_number: str | None = None
    telegram_2fa_password: str | None = None
    telegram_session_string: str | None = None

    # Database
    database_url: str = Field(min_length=1)
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800
    db_pool_timeout: int = 30

    # Media (MVP: local only; S3 is not implemented)
    media_storage_backend: str = "local"
    media_local_path: str = "/data/media"
    media_tmp_path: str = "/tmp/media"

    # Worker / runtime
    task_poll_interval_seconds: float = 2.0
    listener_reload_interval_seconds: float = 30.0
    media_max_retries: int = 3
    media_retry_backoff_base_seconds: float = 5.0
    task_stale_processing_minutes: int = 15
    max_media_bytes: int = 50 * 1024 * 1024
    backfill_on_activate_limit: int = 200
    ai_process_enabled: bool = False

    # API
    api_auth_token: str = Field(min_length=1)

    # Misc
    tz: str = "Europe/Moscow"
    log_level: str = "INFO"

    def require_mtproto(self) -> tuple[int, str, str]:
        """Return (api_id, api_hash, session_string) or raise."""
        if not self.telegram_api_id or not self.telegram_api_hash:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
        if not self.telegram_session_string:
            raise RuntimeError("TELEGRAM_SESSION_STRING required (run auth_cli first)")
        return self.telegram_api_id, self.telegram_api_hash, self.telegram_session_string

    def require_media_storage(self) -> None:
        """Fail fast when an unsupported storage backend is configured."""
        backend = (self.media_storage_backend or "local").strip().lower()
        if backend != "local":
            raise RuntimeError(
                f"MEDIA_STORAGE_BACKEND={backend!r} is not supported in MVP; use local"
            )

    def require_api_auth(self) -> str:
        token = (self.api_auth_token or "").strip()
        if not token:
            raise RuntimeError("API_AUTH_TOKEN required for parser API")
        return token

    def secret_values(self) -> list[str]:
        """Values that must be redacted from logs."""
        secrets: list[str] = []
        for value in (
            self.telegram_api_hash,
            self.telegram_session_string,
            self.telegram_2fa_password,
            self.api_auth_token,
        ):
            if value:
                secrets.append(value)
        # DATABASE_URL may embed password — redact full URL if present
        if self.database_url:
            secrets.append(self.database_url)
        return secrets


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Call cache_clear() in tests."""
    return Settings()  # type: ignore[call-arg]
