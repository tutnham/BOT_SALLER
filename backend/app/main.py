"""FastAPI application entrypoint — Phase 0: /health + global error handling."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import dispose_engine, get_db, get_engine, get_session_factory
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
    heartbeat_task = asyncio.create_task(_heartbeat_loop())
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
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    if scheduler is not None:
        # Wait so in-flight jobs can finish before engine disposal.
        scheduler.shutdown(wait=True)
    await dispose_engine()
    logger.info("Zakupki-Bot backend stopped")


settings = get_settings()
app = FastAPI(
    title="Zakupki-Bot",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
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


async def _heartbeat_loop() -> None:
    """Web process heartbeat. Scheduler hb only when cron runs in-process (not split deploy)."""
    settings = get_settings()
    instance_id = os.environ.get("HOSTNAME", "backend-web")
    while True:
        try:
            factory = get_session_factory()
            async with factory() as session:
                from app.services.heartbeat_service import touch_heartbeat

                await touch_heartbeat(
                    session, process_type="web", instance_id=instance_id
                )
                if settings.scheduler_enabled and not settings.scheduler_expected:
                    await touch_heartbeat(
                        session, process_type="scheduler", instance_id=instance_id
                    )
                await session.commit()
        except Exception as exc:
            logger.warning("Heartbeat write failed: {}", type(exc).__name__)
        await asyncio.sleep(15)


@app.get("/live")
async def live() -> dict[str, str]:
    """Process is up. Does not touch the database."""
    return {"status": "ok"}


@app.get("/ready", response_model=None)
async def ready() -> JSONResponse | dict[str, object]:
    """Database, schema, and required process heartbeats. HTTP 503 when not ready."""
    from app.services.ops_status_service import readiness

    try:
        factory = get_session_factory()
        async with factory() as session:
            ok, body = await readiness(session)
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reasons": ["database"]},
        )
    if not ok:
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/health/details", response_model=None)
async def health_details(
    session: AsyncSession = Depends(get_db),
    x_internal_token: str | None = Header(default=None),
) -> JSONResponse | dict[str, object]:
    from app.services.ops_status_service import details_token_ok, health_details

    if not details_token_ok(x_internal_token):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await health_details(session)


@app.get("/metrics", response_model=None)
async def metrics(
    session: AsyncSession = Depends(get_db),
    x_metrics_token: str | None = Header(default=None, alias="X-Metrics-Token"),
) -> PlainTextResponse | JSONResponse:
    from app.services.metrics_service import render_metrics
    from app.services.ops_status_service import metrics_token_ok

    if not metrics_token_ok(x_metrics_token):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    body = await render_metrics(session)
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")


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
