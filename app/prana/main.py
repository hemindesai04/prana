from contextlib import asynccontextmanager
import time
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request

from prana.config import Settings, load_settings
from prana.db import bootstrap_users, get_connection
from prana.ingestor.router import router as ingestor_router
from prana.logging_setup import configure_logging, get_logger
from prana.maintenance.backup import run_pg_backup

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
        if cfg.bootstrap_users and getattr(app.state, "conn_factory", None) is None:
            with get_connection(cfg.database_url) as conn:
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
            )
            scheduler.start()
            logger.info("Started background scheduler (backup hour=%d)", cfg.backup_cron_hour)

        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    app = FastAPI(title="Prana Health Hub", version="0.2.0", lifespan=lifespan)
    app.state.settings = cfg
    app.state.conn_factory = None
    app.include_router(ingestor_router)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "HTTP %s %s",
            request.method,
            request.url.path,
            extra={
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    return app


app = create_app()
