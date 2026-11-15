import hashlib
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

import psycopg

from prana.logging_setup import get_logger

logger = get_logger(__name__)


def hash_token(token: str) -> str:
    """Return SHA-256 hex digest of an ingest token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@contextmanager
def get_connection(database_url: str) -> Iterator[psycopg.Connection]:
    """Yield a PostgreSQL connection."""
    with psycopg.connect(database_url) as conn:
        yield conn


def bootstrap_users(
    conn: psycopg.Connection,
    users: List[Tuple[str, str]],
    default_tz: str = "UTC",
) -> int:
    """Ensure bootstrap users exist in `users` table; returns count upserted."""
    if not users:
        return 0
    count = 0
    with conn.cursor() as cur:
        for slug, token in users:
            cur.execute(
                """
                INSERT INTO users (slug, display_name, ingest_token_hash, timezone)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    ingest_token_hash = EXCLUDED.ingest_token_hash
                """,
                (slug, slug.capitalize(), hash_token(token), default_tz),
            )
            count += 1
    conn.commit()
    logger.info("Bootstrapped %d user(s)", count)
    return count


def resolve_user_id(conn: psycopg.Connection, token: str) -> Optional[int]:
    """Look up user_id by plaintext ingest token."""
    if not token:
        return None
    token_digest = hash_token(token)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE ingest_token_hash = %s",
            (token_digest,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None
