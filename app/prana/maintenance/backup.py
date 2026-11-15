from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional

import psycopg

from prana.logging_setup import get_logger

logger = get_logger(__name__)


def _record_backup_run(
    database_url: str,
    started_at: datetime,
    status: str,
    file_path: Optional[str] = None,
    size_bytes: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO backup_runs (started_at, finished_at, status, file_path, size_bytes, error)
                    VALUES (%s, NOW(), %s, %s, %s, %s)
                    """,
                    (started_at, status, file_path, size_bytes, error),
                )
            conn.commit()
    except Exception as exc:
        logger.error("Failed to record backup_runs entry: %s", exc)


def run_pg_backup(database_url: str, backup_dir: str, retention_days: int) -> Path:
    """Stream pg_dump (-Fc) to a temp file, chmod 600, rename atomically, and prune old dumps."""
    started_at = datetime.now(timezone.utc)
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    tmp_file = out_dir / f"prana_{stamp}.dump.tmp"
    out_file = out_dir / f"prana_{stamp}.dump"

    try:
        old_umask = os.umask(0o077)
        try:
            subprocess.run(
                ["pg_dump", "-Fc", "-f", str(tmp_file), database_url],
                capture_output=True,
                text=True,
                check=True,
            )
        finally:
            os.umask(old_umask)
        os.chmod(tmp_file, 0o600)
        tmp_file.replace(out_file)
        size_bytes = out_file.stat().st_size
    except Exception as exc:
        if tmp_file.exists():
            tmp_file.unlink(missing_ok=True)
        err_msg = getattr(exc, "stderr", None) or str(exc)
        _record_backup_run(database_url, started_at, "failed", error=err_msg)
        logger.critical("Database backup failed: %s", err_msg)
        raise

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for pattern in ("prana_*.dump", "prana_*.sql.gz"):
        for path in out_dir.glob(pattern):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink()

    _record_backup_run(
        database_url,
        started_at,
        "success",
        file_path=str(out_file),
        size_bytes=size_bytes,
    )
    logger.info("Database backup written to %s (%d bytes)", out_file, size_bytes)
    return out_file


def restore_pg_backup(database_url: str, dump_file: str) -> None:
    """Restore a custom-format (.dump) backup using pg_restore."""
    subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", database_url, dump_file],
        check=True,
    )
    logger.info("Database restored from %s", dump_file)


if __name__ == "__main__":
    db_url = os.environ["DATABASE_URL"]
    if len(sys.argv) >= 3 and sys.argv[1] == "restore":
        restore_pg_backup(db_url, sys.argv[2])
    else:
        run_pg_backup(
            db_url,
            os.getenv("BACKUP_DIR", "/backups"),
            int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
        )
