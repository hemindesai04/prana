from contextlib import asynccontextmanager
import time
from typing import Optional
import uuid

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from prana.config import Settings, load_settings
from prana.db import bootstrap_users, create_pool
from prana.ingestor.router import router as ingestor_router
from prana.logging_setup import configure_logging, get_logger
from prana.maintenance.backup import run_pg_backup
from prana.migrations import run_migrations

logger = get_logger(__name__)


def create_app(
    settings: Optional[Settings] = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    """Build and configure the Prana FastAPI application."""
    configure_logging()
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = None
        if getattr(app.state, "conn_factory", None) is None:
            if cfg.run_migrations_on_startup:
                run_migrations(cfg.database_url)
            pool = create_pool(cfg.database_url)
            app.state.pool = pool
            if cfg.bootstrap_users:
                with pool.connection() as conn:
                    bootstrap_users(conn, cfg.bootstrap_users, cfg.timezone)

        scheduler: Optional[BackgroundScheduler] = None
        if enable_scheduler:
            scheduler = BackgroundScheduler(timezone=cfg.timezone)
            scheduler.add_job(
                run_pg_backup,
                trigger="cron",
                hour=cfg.backup_cron_hour,
                minute=0,
                args=[cfg.database_url, cfg.backup_dir, cfg.backup_retention_days],
                id="nightly_pg_backup",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=3600 * 6,
            )
            scheduler.start()
            app.state.scheduler = scheduler
            logger.info("Started background scheduler (backup hour=%d)", cfg.backup_cron_hour)

        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            if pool is not None:
                pool.close()

    app = FastAPI(title="Prana Health Hub", version="0.3.0", lifespan=lifespan)
    app.state.settings = cfg
    app.state.conn_factory = None
    app.state.pool = None
    app.state.scheduler = None
    app.include_router(ingestor_router)

    @app.middleware("http")
    async def request_guard_and_logging(request: Request, call_next):
        request_id = str(uuid.uuid4())
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > cfg.max_request_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Payload too large", "request_id": request_id},
                    )
            except ValueError:
                pass

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.exception(
                "Unhandled exception on %s %s",
                request.method,
                request.url.path,
                extra={
                    "request_id": request_id,
                    "path": request.url.path,
                    "status_code": 500,
                    "duration_ms": duration_ms,
                },
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "request_id": request_id},
            )

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        if not (request.url.path == "/health" and response.status_code == 200):
            logger.info(
                "HTTP %s %s",
                request.method,
                request.url.path,
                extra={
                    "request_id": request_id,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        return response

    return app


app = create_app()
