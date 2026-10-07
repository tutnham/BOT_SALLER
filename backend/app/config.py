"""Application settings loaded from environment / .env (fail-fast on required secrets)."""

import re
from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TELEGRAM_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class Settings(BaseSettings):
    """Runtime configuration. Missing required fields raise ValidationError at startup."""

    model_config = SettingsConfigDict(
        # Prefer backend/.env; fall back to repo-root .env when running from backend/
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    # Required — fail-fast if absent/empty (env: TELEGRAM_BOT_TOKEN, DATABASE_URL, ...)
    telegram_bot_token: str = Field(min_length=1)
    database_url: str = Field(min_length=1)
    webhook_secret: str = Field(min_length=1)
    telegram_webhook_secret_token: str = Field(min_length=1)

    # Optional operational settings
    admin_alert_chat_id: int | None = None
    analytics_chat_id: int | None = None
    confidence_threshold: float = 0.75
    supplier_bind_min_score: int = 3
    supplier_bind_min_margin: int = 2
    supplier_reply_max_age_hours: int = 48
    supplier_bind_llm_threshold: float = 0.85
    # Window for retrying unbound supplier price messages after a new RFQ (0 = off).
    supplier_rebind_window_hours: int = 24
    supplier_bind_prompt_ttl_hours: int = 4
    supplier_bind_session_ttl_hours: int = 4
    supplier_correction_window_minutes: int | None = 30
    supplier_single_candidate_auto_bind_enabled: bool = False
    supplier_bind_buttons_enabled: bool = False
    suppliers_without_categories_policy: str = "skip_and_notify"
    tz: str = "Europe/Moscow"
    # Used for NL synonym @mention gating in groups (Phase 5). Optional.
    telegram_bot_username: str | None = None
    # Public HTTPS URL for setWebhook (ops); not required at runtime.
    public_backend_url: str | None = None
    # Disable when running extra workers/replicas that must not double-fire cron.
    scheduler_enabled: bool = True
    # Web without an in-process scheduler still expects a scheduler process heartbeat.
    scheduler_expected: bool = False
    app_version: str = "0.1.0"
    commit_sha: str = "unknown"
    heartbeat_stale_seconds: int = 90
    health_details_token: str | None = None
    metrics_token: str = Field(min_length=1)
    retention_dry_run: bool = True
    retention_inbox_days: int = 30
    retention_outbox_days: int = 30
    retention_parse_cache_days: int = 14
    retention_batch_size: int = 500

    # Morning price (Phase 4)
    # Legacy single-chat setting; kept for backward compatibility with existing deployments.
    price_approval_chat_id: int | None = None
    # Preferred: CSV of chat ids so multiple people can approve price drafts.
    price_approval_chat_ids: str | None = None
    price_publish_chat_ids: str | None = None
    default_markup: Decimal = Decimal("500")

    # LLM (optional until Phase 3)
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_provider: str | None = None

    # LLM billing reminder (monthly). Static template; no LLM text generation.
    llm_provider_name: str = "DeepSeek"
    # Seed/bootstrap value only; runtime source of truth is app_settings table.
    llm_topup_price_text: str | None = None
    llm_topup_payment_url: str | None = None
    llm_billing_reminder_chat_id: int | None = None
    llm_billing_reminder_day: int = 1

    # Parser microservice (Phase 4)
    parser_api_url: str = "http://tg-parser-api:8000"
    parser_api_token: str | None = None
    parser_timeout_seconds: float = 15.0
    llm_timeout_seconds: float = 30.0
    llm_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://127.0.0.1:11434"

    # Pool tuning
    db_pool_size: int = Field(default=5)
    db_max_overflow: int = Field(default=10)
    db_pool_recycle: int = Field(default=1800)
    db_pool_timeout: int = Field(default=30)
    # Scheduler uses a dedicated small pool so long jobs cannot starve web workers.
    db_scheduler_pool_size: int = Field(default=2)
    db_scheduler_max_overflow: int = Field(default=3)
    db_scheduler_pool_timeout: int = Field(default=30)

    # Operational safety
    log_level: str = "INFO"
    webhook_max_body_bytes: int = 262_144
    max_message_text_len: int = 4000
    docs_enabled: bool = False

    # Async webhook / outbox rollout (mutually exclusive with sync path when enabled)
    webhook_async_enabled: bool = False
    outbox_delivery_enabled: bool = False

    # Dedicated worker (Coolify service backend-worker)
    worker_poll_interval_seconds: float = 1.0
    worker_lease_seconds: int = 60
    worker_max_attempts: int = 8
    worker_inbox_batch_size: int = 10
    worker_outbox_batch_size: int = 20
    worker_id: str | None = None
    worker_shutdown_grace_seconds: float = 20.0
    # Test-only delay after a lease is committed. Production leaves this at 0.
    worker_test_pause_seconds: float = 0.0

    # Morning price LLM parse concurrency cap
    morning_price_llm_concurrency: int = 4

    @staticmethod
    def _parse_csv_chat_ids(raw: str | None) -> list[int]:
        raw = (raw or "").strip()
        if not raw:
            return []
        result: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            result.append(int(part))
        return result

    @property
    def price_publish_chat_id_list(self) -> list[int]:
        """Parse CSV of chat ids for approved price list publication."""
        return self._parse_csv_chat_ids(self.price_publish_chat_ids)

    @property
    def price_approval_chat_id_list(self) -> list[int]:
        """Chats notified for price draft approval.

        Prefers ``PRICE_APPROVAL_CHAT_IDS`` (CSV, supports several approvers);
        falls back to the legacy single ``PRICE_APPROVAL_CHAT_ID``, then to
        ``ADMIN_ALERT_CHAT_ID`` if neither is set. Draft notify also adds
        owners with ``dm_ok`` (see ``resolve_price_draft_destinations``).
        """
        csv_ids = self._parse_csv_chat_ids(self.price_approval_chat_ids)
        if csv_ids:
            return csv_ids
        if self.price_approval_chat_id is not None:
            return [self.price_approval_chat_id]
        if self.admin_alert_chat_id is not None:
            return [self.admin_alert_chat_id]
        return []

    @field_validator("default_markup", mode="before")
    @classmethod
    def _coerce_default_markup(cls, value: object) -> object:
        if value is None or value == "":
            return Decimal("500")
        return value

    @field_validator("telegram_webhook_secret_token")
    @classmethod
    def _validate_telegram_secret_token(cls, value: str) -> str:
        if not _TELEGRAM_SECRET_TOKEN_RE.fullmatch(value):
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET_TOKEN must match "
                r"^[A-Za-z0-9_-]{1,256}$ (Telegram setWebhook secret_token)"
            )
        return value

    @field_validator("confidence_threshold", "supplier_bind_llm_threshold")
    @classmethod
    def _validate_unit_interval(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError("confidence thresholds must be in (0, 1]")
        return value

    @field_validator("supplier_reply_max_age_hours", "supplier_bind_session_ttl_hours")
    @classmethod
    def _validate_positive_hours(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("hour-based supplier settings must be positive")
        return value

    @field_validator("supplier_correction_window_minutes")
    @classmethod
    def _validate_correction_window(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("SUPPLIER_CORRECTION_WINDOW_MINUTES must be positive when set")
        return value

    @field_validator("llm_billing_reminder_day")
    @classmethod
    def _validate_llm_billing_reminder_day(cls, value: int) -> int:
        if not 1 <= value <= 28:
            raise ValueError(
                "LLM_BILLING_REMINDER_DAY must be between 1 and 28 inclusive; "
                "APScheduler does not shift the cron day to the last day of the month."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()  # type: ignore[call-arg]
