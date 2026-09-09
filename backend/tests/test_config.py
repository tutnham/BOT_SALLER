"""Settings fail-fast and defaults."""

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_settings_missing_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_reject_empty_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            telegram_bot_token="",
            database_url="postgresql+asyncpg://u:p@localhost/db",
            webhook_secret="sec",
            telegram_webhook_secret_token="sec-telegram",
        )


def test_settings_loads_required_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("WEBHOOK_SECRET", "secret-xyz")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "secret-telegram-xyz")
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.telegram_bot_token == "tok-abc"
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.webhook_secret == "secret-xyz"
    assert s.telegram_webhook_secret_token == "secret-telegram-xyz"
    assert s.confidence_threshold == 0.75
    assert s.tz == "Europe/Moscow"
    assert s.parser_api_url == "http://tg-parser-api:8000"
    assert s.scheduler_enabled is True
    assert s.docs_enabled is False
    assert s.public_backend_url is None


def test_settings_reject_invalid_telegram_secret_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            telegram_bot_token="tok",
            database_url="postgresql+asyncpg://u:p@localhost/db",
            webhook_secret="sec",
            telegram_webhook_secret_token="bad token!",
        )


def test_settings_llm_billing_reminder_day_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="tok",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        webhook_secret="sec",
        telegram_webhook_secret_token="sec-telegram",
        llm_billing_reminder_day=1,
    )
    assert s.llm_billing_reminder_day == 1

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            telegram_bot_token="tok",
            database_url="postgresql+asyncpg://u:p@localhost/db",
            webhook_secret="sec",
            telegram_webhook_secret_token="sec-telegram",
            llm_billing_reminder_day=31,
        )


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "sec-telegram")
    a = get_settings()
    b = get_settings()
    assert a is b
    get_settings.cache_clear()
