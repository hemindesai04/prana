import os
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    database_url: str
    admin_token: str
    bootstrap_users: List[Tuple[str, str]]
    backup_dir: str
    backup_cron_hour: int
    backup_retention_days: int
    timezone: str


def parse_bootstrap_users(raw: str) -> List[Tuple[str, str]]:
    """Parse comma-separated `slug:token` pairs into a list of tuples."""
    pairs: List[Tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        slug, token = item.split(":", 1)
        slug, token = slug.strip(), token.strip()
        if slug and token:
            pairs.append((slug, token))
    return pairs


def load_settings() -> Settings:
    """Load settings from process environment."""
    return Settings(
        database_url=os.getenv("DATABASE_URL", "postgresql://prana:prana@db:5432/prana"),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
        bootstrap_users=parse_bootstrap_users(os.getenv("BOOTSTRAP_USERS", "")),
        backup_dir=os.getenv("BACKUP_DIR", "/backups"),
        backup_cron_hour=int(os.getenv("BACKUP_CRON_HOUR", "3")),
        backup_retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
        timezone=os.getenv("TZ", "UTC"),
    )
