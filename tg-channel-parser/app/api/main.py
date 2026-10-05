"""FastAPI application for tg-channel-parser."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Response
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers import channels, posts
from app.config import get_settings
from app.db.session import dispose_engine, get_db, get_engine
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


@app.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(session: AsyncSession = Depends(get_db)) -> JSONResponse | dict[str, object]:
    from app.services.ops_status_service import readiness

    ok, body = await readiness(session)
    if not ok:
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/health/details")
async def health_details_route(
    session: AsyncSession = Depends(get_db),
    x_internal_token: str | None = Header(default=None),
) -> JSONResponse | dict[str, object]:
    from app.services.ops_status_service import details_token_ok, health_details as build_details

    if not details_token_ok(x_internal_token):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await build_details(session)


@app.get("/health")
async def health() -> Response:
    """Legacy probe: always HTTP 200; db field reflects connectivity."""
    db_status = "ok"
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Health DB check failed: {}", exc)
        db_status = "error"
    body = {"status": "ok", "db": db_status}
    return Response(
        content=json.dumps(body),
        status_code=200,
        media_type="application/json",
    )
