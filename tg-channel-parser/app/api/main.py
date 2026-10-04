"""FastAPI application for tg-channel-parser."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from loguru import logger
from sqlalchemy import text

from app.api.routers import channels, posts
from app.config import get_settings
from app.db.session import dispose_engine, get_engine
from app.logging_setup import setup_logging


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    settings.require_api_auth()
    settings.require_media_storage()
    yield
    await dispose_engine()


app = FastAPI(title="tg-channel-parser", version="1.0.0", lifespan=lifespan)
app.include_router(channels.router)
app.include_router(posts.router)


@app.get("/health")
async def health() -> Response:
    """Liveness/readiness: process up and Postgres reachable."""
    db_status = "ok"
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Health DB check failed: {}", exc)
        db_status = "error"
    body = {"status": "ok" if db_status == "ok" else "degraded", "db": db_status}
    status_code = 200 if db_status == "ok" else 503
    return Response(
        content=json.dumps(body),
        status_code=status_code,
        media_type="application/json",
    )
