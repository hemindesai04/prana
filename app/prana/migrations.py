from pathlib import Path
from typing import Optional, Tuple

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg

from prana.logging_setup import get_logger

logger = get_logger(__name__)

MIGRATION_LOCK_KEY = 8842001


def _alembic_config(database_url: str) -> Config:
    app_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(app_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(app_root / "alembic"))
    sa_url = database_url
    if sa_url.startswith("postgresql://"):
        sa_url = "postgresql+psycopg://" + sa_url[len("postgresql://") :]
    cfg.set_main_option("sqlalchemy.url", sa_url)
    return cfg


def run_migrations(database_url: str) -> None:
    """Run Alembic upgrade head under a PostgreSQL advisory lock."""
    cfg = _alembic_config(database_url)
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            command.upgrade(cfg, "head")
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
            conn.commit()
    logger.info("Alembic migrations upgraded to head")


def get_migration_status(conn: psycopg.Connection, database_url: str) -> Tuple[Optional[str], Optional[str], bool]:
    """Return (current_revision, head_revision, is_at_head)."""
    cfg = _alembic_config(database_url)
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    current: Optional[str] = None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.alembic_version')"
        )
        exists = cur.fetchone()
        if exists and exists[0] is not None:
            cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = cur.fetchone()
            if row:
                current = str(row[0])
    return current, head, (current == head and head is not None)
