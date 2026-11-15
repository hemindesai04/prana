import gzip
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prana.logging_setup import get_logger

logger = get_logger(__name__)


def run_pg_backup(database_url: str, backup_dir: str, retention_days: int) -> Path:
    """Run pg_dump into a gzipped file and prune backups older than retention_days."""
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"prana_{stamp}.sql.gz"

    proc = subprocess.run(
        ["pg_dump", database_url],
        capture_output=True,
        check=True,
    )
    with gzip.open(out_file, "wb") as gz:
        gz.write(proc.stdout)

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for path in out_dir.glob("prana_*.sql.gz"):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            path.unlink()

    logger.info("Database backup written to %s", out_file)
    return out_file
