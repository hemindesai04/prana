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
    max_request_bytes: int = 50 * 1024 * 1024
    run_migrations_on_startup: bool = True
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    morning_briefing_hour: int = 8
    evening_briefing_hour: int = 22


def parse_bootstrap_users(raw: str) -> List[Tuple[str, str]]:
    """Parse comma-separated `slug:token` pairs and reject duplicate tokens or slugs."""
    pairs: List[Tuple[str, str]] = []
    seen_slugs: set[str] = set()
    seen_tokens: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        slug, token = item.split(":", 1)
        slug, token = slug.strip(), token.strip()
        if not slug or not token:
            continue
        if slug in seen_slugs:
            raise ValueError(f"Duplicate slug in BOOTSTRAP_USERS: {slug}")
        if token in seen_tokens:
            raise ValueError(f"Duplicate ingest token in BOOTSTRAP_USERS for slug: {slug}")
        seen_slugs.add(slug)
        seen_tokens.add(token)
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
        max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", str(50 * 1024 * 1024))),
        run_migrations_on_startup=os.getenv("RUN_MIGRATIONS_ON_STARTUP", "true").lower()
        in ("1", "true", "yes"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
        morning_briefing_hour=int(os.getenv("MORNING_BRIEFING_HOUR", "8")),
        evening_briefing_hour=int(os.getenv("EVENING_BRIEFING_HOUR", "22")),
    )
