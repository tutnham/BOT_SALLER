"""Pytest fixtures: Postgres test DB, Alembic migrations, transactional sessions."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# Force test secrets so host/CI env cannot desync hardcoded headers in tests.
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["TELEGRAM_WEBHOOK_SECRET_TOKEN"] = "test-telegram-webhook-secret"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://zakupki:changeme@127.0.0.1:5432/zakupki",
)
os.environ.setdefault("PRICE_APPROVAL_CHAT_ID", "-1001111111111")
os.environ.setdefault("PRICE_PUBLISH_CHAT_IDS", "-1002222222222")
os.environ.setdefault("PARSER_API_TOKEN", "test-parser-token")
os.environ.setdefault("DEFAULT_MARKUP", "500")

from app.config import get_settings  # noqa: E402
from app.db.models import Employee, MarkupRule, Owner, Supplier  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.llm.client import set_llm_client  # noqa: E402
from app.main import app  # noqa: E402
from app.telegram.client import set_telegram_client  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_SECRET = "test-webhook-secret"
TELEGRAM_WEBHOOK_SECRET_TOKEN = "test-telegram-webhook-secret"
PRICE_APPROVAL_CHAT_ID = -1001111111111
PRICE_PUBLISH_CHAT_ID = -1002222222222



class MockTelegramClient:
    """Records outbound messages; returns monotonic fake message ids."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict[str, Any] | None]] = []
        self.parse_modes: list[str | None] = []
        self.answer_callbacks: list[tuple[str, str | None, bool]] = []
        self.edited: list[tuple[int, int, str, dict[str, Any] | None]] = []
        self.chats: dict[int, dict[str, Any]] = {}
        self._next_id = 9000

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        self._next_id += 1
        self.sent.append((chat_id, text, reply_markup))
        self.parse_modes.append(parse_mode)
        return self._next_id

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self.answer_callbacks.append((callback_query_id, text, show_alert))

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.edited.append((chat_id, message_id, text, reply_markup))

    def set_chat(self, chat_id: int, info: dict[str, Any]) -> None:
        self.chats[chat_id] = info

    async def get_chat(self, chat_id: int) -> dict[str, Any] | None:
        return self.chats.get(chat_id)


class MockLLMClient:
    """Mock LLM client with configurable handlers and call counters."""

    def __init__(self) -> None:
        self.parse_calls = 0
        self.normalize_calls = 0
        self.price_calls = 0
        self.report_calls = 0
        self.parse_result = {
            "available": None,
            "qty": None,
            "price": None,
            "min_sale_price": None,
            "condition": None,
            "note": None,
            "confidence": 0.0,
        }
        self.normalize_result = {
            "model": "",
            "storage": None,
            "color": None,
            "region": None,
            "sim": None,
            "qty": None,
            "condition": None,
            "confidence": 0.0,
        }
        self.price_result = {
            "items": [
                {
                    "model": "iPhone 15",
                    "storage": "256GB",
                    "color": "Black",
                    "region": None,
                    "sim": None,
                    "condition": None,
                    "price": 70000,
                    "currency": "RUB",
                    "confidence": 0.95,
                }
            ]
        }
        self.report_result = "<b>Отчет</b>"
        self.raise_parse: Exception | None = None
        self.raise_normalize: Exception | None = None
        self.raise_price: Exception | None = None
        self.raise_report: Exception | None = None

    async def parse_supplier_reply(self, raw_text: str):
        from app.llm.schemas import ParsedSupplierReply

        self.parse_calls += 1
        if self.raise_parse is not None:
            raise self.raise_parse
        return ParsedSupplierReply.model_validate(self.parse_result)

    async def normalize_request(self, raw_text: str):
        from app.llm.schemas import NormalizedRequest

        self.normalize_calls += 1
        if self.raise_normalize is not None:
            raise self.raise_normalize
        return NormalizedRequest.model_validate(self.normalize_result)

    async def parse_price_list(self, raw_text: str):
        from app.llm.schemas import validate_price_list_payload

        self.price_calls += 1
        if self.raise_price is not None:
            raise self.raise_price
        return validate_price_list_payload(self.price_result)

    async def format_report(self, metrics: dict):
        self.report_calls += 1
        if self.raise_report is not None:
            raise self.raise_report
        return self.report_result


@pytest.fixture
def webhook_headers() -> dict[str, str]:
    return {
        "X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET_TOKEN
    }


@pytest.fixture
def job_webhook_headers() -> dict[str, str]:
    return {"X-Webhook-Secret": WEBHOOK_SECRET}


@pytest.fixture
def mock_telegram() -> MockTelegramClient:
    return MockTelegramClient()


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.fixture
def seed_group_chat_id() -> int:
    return -1001234567890


@pytest.fixture
def seed_employee_telegram_id() -> int:
    return 100100100


@pytest_asyncio.fixture
async def seed_employee(
    db_session: AsyncSession,
    seed_employee_telegram_id: int,
) -> Employee:
    employee = Employee(
        telegram_id=seed_employee_telegram_id,
        name="Test Employee",
        active=True,
    )
    db_session.add(employee)
    await db_session.flush()
    return employee


@pytest_asyncio.fixture
async def seed_suppliers(db_session: AsyncSession) -> list[Supplier]:
    suppliers = [
        Supplier(
            telegram_id=200200201,
            name="Supplier One",
            active=True,
            dm_ok=True,
        ),
        Supplier(
            telegram_id=200200202,
            name="Supplier Two",
            active=True,
            dm_ok=True,
        ),
        Supplier(
            telegram_id=200200203,
            name="Supplier Inactive",
            active=False,
            dm_ok=True,
        ),
        Supplier(
            telegram_id=200200204,
            name="Supplier No DM",
            active=True,
            dm_ok=False,
        ),
    ]
    db_session.add_all(suppliers)
    await db_session.flush()
    return suppliers


@pytest_asyncio.fixture
async def seed_owner(db_session: AsyncSession) -> Owner:
    owner = Owner(telegram_id=300300300, name="Test Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()
    return owner


@pytest_asyncio.fixture
async def seed_channel_supplier(db_session: AsyncSession) -> Supplier:
    supplier = Supplier(
        telegram_id=200200299,
        name="Channel Supplier",
        active=True,
        dm_ok=True,
        price_channel_id=-1005555555555,
        price_channel_username="@channel_prices",
        last_price_sync_at=None,
    )
    db_session.add(supplier)
    await db_session.flush()
    return supplier


@pytest_asyncio.fixture
async def seed_markup_rules(db_session: AsyncSession) -> list[MarkupRule]:
    from decimal import Decimal

    rules = [
        MarkupRule(
            category="apple",
            markup_fixed=Decimal("700"),
            active=True,
        ),
        MarkupRule(
            category="*",
            markup_fixed=None,
            markup_min=Decimal("500"),
            markup_max=Decimal("1000"),
            active=True,
        ),
    ]
    db_session.add_all(rules)
    await db_session.flush()
    return rules


@pytest.fixture
def seed_supplier_count() -> int:
    """Eligible suppliers: active + telegram_id + dm_ok."""
    return 2


@pytest_asyncio.fixture
async def webhook_client(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
    seed_employee: Employee,
    seed_suppliers: list[Supplier],
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with DB session override and mocked Telegram."""

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    set_telegram_client(mock_telegram)
    set_llm_client(mock_llm)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    set_telegram_client(None)
    set_llm_client(None)


def _admin_and_test_urls(database_url: str) -> tuple[str, str]:
    """Derive admin URL (postgres db) and test DB URL (zakupki_test)."""
    parsed = urlparse(database_url)
    admin_url = urlunparse(parsed._replace(path="/postgres"))
    test_url = urlunparse(parsed._replace(path="/zakupki_test"))
    return admin_url, test_url


async def _ensure_test_database(admin_url: str, test_db_name: str = "zakupki_test") -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_db_name},
            )
            if exists.scalar_one_or_none() is None:
                await conn.execute(text(f'CREATE DATABASE "{test_db_name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> Generator[str, None, None]:
    base_url = os.environ.get(
        "TEST_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://zakupki:changeme@127.0.0.1:5432/zakupki",
        ),
    )
    admin_url, test_url = _admin_and_test_urls(base_url)

    import asyncio

    asyncio.run(_ensure_test_database(admin_url))

    os.environ["DATABASE_URL"] = test_url
    get_settings.cache_clear()
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "app" / "migrations"))
    try:
        command.downgrade(cfg, "base")
    except Exception:
        pass
    command.upgrade(cfg, "head")

    yield test_url

    get_settings.cache_clear()


@pytest_asyncio.fixture
async def engine(test_database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """
    Function-scoped engine with NullPool.

    Avoids asyncpg 'Future attached to a different loop' when pytest-asyncio
    creates a new event loop per test.
    """
    from app.db import session as session_mod
    from sqlalchemy.ext.asyncio import async_sessionmaker

    eng = create_async_engine(test_database_url, poolclass=NullPool)
    session_mod._engine = eng
    session_mod._session_factory = async_sessionmaker(
        eng, class_=AsyncSession, expire_on_commit=False
    )
    yield eng
    await eng.dispose()
    session_mod._engine = None
    session_mod._session_factory = None


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Each test runs inside a connection-level transaction that is rolled back."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client against the FastAPI app (DB engine already pointed at test DB)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
