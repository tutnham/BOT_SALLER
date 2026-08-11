"""FastAPI application entrypoint — Phase 0: /health + global error handling."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text

from app.config import get_settings
from app.db.session import dispose_engine, get_engine
from app.middleware.rate_limit import rate_limit_webhook_and_jobs
from app.scheduler import setup_scheduler
from app.telegram.jobs_router import router as jobs_router
from app.telegram.webhook_router import router as telegram_router


def _configure_logging() -> None:
    """Structured logging; avoid dumping secret values into log lines."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=get_settings().log_level.upper(),
        format=(
            "{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level} | {name}:{function}:{line} | "
            "{message}"
        ),
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    # Redact known secret values if they ever appear in a message
    try:
        settings = get_settings()
        secrets = [
            settings.telegram_bot_token,
            settings.webhook_secret,
            settings.telegram_webhook_secret_token,
            settings.llm_api_key,
            settings.parser_api_token,
        ]
        secret_values = [s for s in secrets if s]

        def _patcher(record: Any) -> None:
            msg = record["message"]
            for secret in secret_values:
                if secret and secret in msg:
                    msg = msg.replace(secret, "***")
            record["message"] = msg

        logger.configure(patcher=_patcher)  # type: ignore[arg-type]
    except Exception:
        # Settings may be unavailable in some test contexts — logging still works
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    # Fail-fast: required env must be present at startup
    settings = get_settings()
    get_engine()
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = setup_scheduler()
        scheduler.start()
        logger.info(
            "Starting Zakupki-Bot backend (tz={}, confidence_threshold={}, scheduler=on)",
            settings.tz,
            settings.confidence_threshold,
        )
    else:
        logger.info(
            "Starting Zakupki-Bot backend (tz={}, confidence_threshold={}, scheduler=off)",
            settings.tz,
            settings.confidence_threshold,
        )
    yield
    if scheduler is not None:
        # Wait so in-flight jobs can finish before engine disposal.
        scheduler.shutdown(wait=True)
    await dispose_engine()
    logger.info("Zakupki-Bot backend stopped")


app = FastAPI(
    title="Zakupki-Bot",
    version="0.1.0",
    lifespan=lifespan,
)
app.middleware("http")(rate_limit_webhook_and_jobs)


@app.middleware("http")
async def enforce_request_body_limit(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    max_bytes = get_settings().webhook_max_body_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "payload_too_large"},
                )
        except ValueError:
            pass
    body = await request.body()
    if len(body) > max_bytes:
        return JSONResponse(status_code=413, content={"detail": "payload_too_large"})
    request._body = body  # type: ignore[attr-defined]
    return await call_next(request)


app.include_router(telegram_router)
app.include_router(jobs_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Log unexpected errors; return 500 without crashing the process."""
    logger.exception(
        "Unhandled exception on {} {}: {}",
        request.method,
        request.url.path,
        exc,
    )
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": "internal_server_error"},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    """
    Liveness/readiness probe (TECH DOC §7.5).
    No auth. Returns db status without raising 500 if DB is unreachable.
    """
    db_status = "ok"
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Health DB check failed: {}", exc)
        db_status = "error"

    return {"status": "ok", "db": db_status}
