"""FastAPI application for tg-channel-parser."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import channels, posts
from app.config import get_settings
from app.db.session import dispose_engine
from app.logging_setup import setup_logging


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    yield
    await dispose_engine()


app = FastAPI(title="tg-channel-parser", version="1.0.0", lifespan=lifespan)
app.include_router(channels.router)
app.include_router(posts.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
