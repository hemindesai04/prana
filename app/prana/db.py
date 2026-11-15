import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg_pool import ConnectionPool

from prana.logging_setup import get_logger

logger = get_logger(__name__)


def hash_token(token: str) -> str:
    """Return SHA-256 hex digest of an ingest token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compute_payload_sha256(payload: Dict[str, Any]) -> str:
    """Return deterministic SHA-256 hex digest of a JSON payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sanitize_json_value(obj: Any) -> Any:
    """Strip NUL bytes recursively so PostgreSQL JSONB never rejects raw payloads."""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, list):
        return [sanitize_json_value(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k).replace("\x00", ""): sanitize_json_value(v) for k, v in obj.items()}
    return obj


def create_pool(database_url: str, min_size: int = 1, max_size: int = 5) -> ConnectionPool:
    """Create a psycopg connection pool."""
    return ConnectionPool(conninfo=database_url, min_size=min_size, max_size=max_size, open=True)


def bootstrap_users(
    conn: psycopg.Connection,
    users: List[Tuple[str, str]],
    default_tz: str = "UTC",
) -> int:
    """Insert missing bootstrap users without overwriting rotated tokens."""
    if not users:
        return 0
    seen_hashes: set[str] = set()
    for slug, token in users:
        digest = hash_token(token)
        if digest in seen_hashes:
            raise ValueError(f"Duplicate ingest token in BOOTSTRAP_USERS for slug={slug}")
        seen_hashes.add(digest)

    inserted = 0
    with conn.cursor() as cur:
        for slug, token in users:
            digest = hash_token(token)
            cur.execute(
                "SELECT slug FROM users WHERE ingest_token_hash = %s AND slug <> %s",
                (digest, slug),
            )
            conflict = cur.fetchone()
            if conflict:
                raise ValueError(
                    f"Bootstrap token for '{slug}' conflicts with existing user '{conflict[0]}'"
                )
            cur.execute(
                """
                INSERT INTO users (slug, display_name, ingest_token_hash, timezone)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (slug) DO NOTHING
                """,
                (slug, slug.capitalize(), digest, default_tz),
            )
            inserted += cur.rowcount or 0
    conn.commit()
    logger.info("Bootstrapped users (inserted=%d, configured=%d)", inserted, len(users))
    return inserted


def resolve_user(conn: psycopg.Connection, token: str) -> Optional[Tuple[int, str]]:
    """Look up (user_id, timezone) by plaintext ingest token."""
    if not token:
        return None
    token_digest = hash_token(token)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, timezone FROM users WHERE ingest_token_hash = %s",
            (token_digest,),
        )
        row = cur.fetchone()
    return (int(row[0]), str(row[1])) if row else None
