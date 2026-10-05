"""Alembic 0008 upgrade/downgrade. Isolated DB when CREATEDB is available."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "zakupki_migrate_0008"


def _urls() -> tuple[str, str]:
    base = os.environ.get(
        "TEST_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://zakupki:changeme@127.0.0.1:5432/zakupki",
        ),
    )
    sync = base.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    parsed = urlparse(sync)
    admin = urlunparse(parsed._replace(path="/postgres"))
    test = urlunparse(parsed._replace(path=f"/{TEST_DB_NAME}"))
    return admin, test


def _alembic_cfg(url: str) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "app" / "migrations"))
    os.environ["DATABASE_URL"] = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    get_settings.cache_clear()
    return cfg


def _can_create_database() -> bool:
    admin_url, _test_url = _urls()
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        return True
    except ProgrammingError:
        return False
    finally:
        engine.dispose()


@pytest.fixture
def isolated_engine() -> Generator[Engine, None, None]:
    previous_url = os.environ.get("DATABASE_URL")
    admin_url, test_url = _urls()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin.dispose()

    engine = create_engine(test_url)
    try:
        yield engine
    finally:
        engine.dispose()
        if previous_url is not None:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(
                text(
                    f"""
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid()
                    """
                )
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        admin.dispose()


@pytest.mark.asyncio
async def test_0008_schema_present_on_head(db_session: AsyncSession) -> None:
    exists = await db_session.scalar(text("SELECT to_regclass('public.business_connections')"))
    assert exists == "business_connections"
    labels = (
        await db_session.scalars(
            text(
                """
                SELECT enumlabel FROM pg_enum
                JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
                WHERE pg_type.typname = 'supplier_chat_type'
                """
            )
        )
    ).all()
    assert "business_dm" in labels


@pytest.mark.skipif(not _can_create_database(), reason="role cannot CREATE DATABASE")
def test_0008_upgrade_downgrade_upgrade(isolated_engine: Engine) -> None:
    _admin_url, test_url = _urls()
    cfg = _alembic_cfg(test_url)

    command.upgrade(cfg, "head")
    with isolated_engine.connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE tablename = 'business_connections'")
        ).scalar_one()
        assert tables == "business_connections"
        enum_vals = conn.execute(
            text(
                """
                SELECT enumlabel FROM pg_enum
                JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
                WHERE pg_type.typname = 'supplier_chat_type'
                ORDER BY enumlabel
                """
            )
        ).scalars().all()
        assert "business_dm" in enum_vals

    command.downgrade(cfg, "0007_price_approval_and_supplier_chats")
    with isolated_engine.connect() as conn:
        missing = conn.execute(
            text("SELECT to_regclass('public.business_connections')")
        ).scalar_one()
        assert missing is None
        enum_vals = conn.execute(
            text(
                """
                SELECT enumlabel FROM pg_enum
                JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
                WHERE pg_type.typname = 'supplier_chat_type'
                ORDER BY enumlabel
                """
            )
        ).scalars().all()
        assert "business_dm" not in enum_vals

    command.upgrade(cfg, "head")
    with isolated_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT to_regclass('public.business_connections')")).scalar_one()
            == "business_connections"
        )
        rls = conn.execute(
            text(
                """
                SELECT relrowsecurity FROM pg_class
                WHERE relname = 'business_connections'
                """
            )
        ).scalar_one()
        assert rls is True
