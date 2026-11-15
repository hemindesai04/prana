from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from prana.db import get_connection, hash_token, resolve_user_id
from prana.ingestor.apple_health import store_apple_health_payload
from prana.ingestor.points import GenericIngestPayload, store_generic_points
from prana.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter()


class CreateUserRequest(BaseModel):
    slug: str
    display_name: str
    ingest_token: str
    timezone: str = "UTC"


def _db_conn(request: Request):
    override = getattr(request.app.state, "conn_factory", None)
    if override is not None:
        return override()
    return get_connection(request.app.state.settings.database_url)


def require_user_id(
    request: Request,
    x_ingest_token: Optional[str] = Header(default=None),
) -> int:
    """Resolve user_id from X-Ingest-Token or raise 401."""
    if not x_ingest_token:
        raise HTTPException(status_code=401, detail="Missing X-Ingest-Token")
    with _db_conn(request) as conn:
        user_id = resolve_user_id(conn, x_ingest_token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid X-Ingest-Token")
    return user_id


def require_admin(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None),
) -> None:
    """Validate X-Admin-Token for administrative endpoints."""
    configured = request.app.state.settings.admin_token
    if not configured or x_admin_token != configured:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")


@router.get("/health")
def health(request: Request) -> Dict[str, str]:
    with _db_conn(request) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    return {"status": "ok"}


@router.post("/api/v1/users", dependencies=[Depends(require_admin)])
def create_user(body: CreateUserRequest, request: Request) -> Dict[str, Any]:
    """Create or update a household user and their ingest token."""
    with _db_conn(request) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (slug, display_name, ingest_token_hash, timezone)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    ingest_token_hash = EXCLUDED.ingest_token_hash,
                    timezone = EXCLUDED.timezone
                RETURNING id
                """,
                (body.slug, body.display_name, hash_token(body.ingest_token), body.timezone),
            )
            user_id = int(cur.fetchone()[0])
        conn.commit()
    logger.info("Upserted user slug=%s", body.slug, extra={"user_id": user_id})
    return {"status": "ok", "user_id": user_id, "slug": body.slug}


@router.post("/api/v1/ingest/apple-health")
def ingest_apple_health(
    payload: Dict[str, Any],
    request: Request,
    user_id: int = Depends(require_user_id),
) -> Dict[str, Any]:
    with _db_conn(request) as conn:
        metrics_count, workouts_count = store_apple_health_payload(conn, user_id, payload)
    logger.info(
        "Ingested apple_health metrics=%d workouts=%d",
        metrics_count,
        workouts_count,
        extra={"user_id": user_id, "source": "apple_health"},
    )
    return {
        "status": "ok",
        "user_id": user_id,
        "metrics_processed": metrics_count,
        "workouts_processed": workouts_count,
    }


@router.post("/api/v1/ingest/points")
def ingest_points(
    payload: GenericIngestPayload,
    request: Request,
    user_id: int = Depends(require_user_id),
) -> Dict[str, Any]:
    with _db_conn(request) as conn:
        count = store_generic_points(conn, user_id, payload)
    logger.info(
        "Ingested generic points=%d",
        count,
        extra={"user_id": user_id, "source": payload.source},
    )
    return {
        "status": "ok",
        "user_id": user_id,
        "source": payload.source,
        "points_processed": count,
    }


@router.get("/api/v1/stats")
def get_stats(
    request: Request,
    user_id: int = Depends(require_user_id),
) -> Dict[str, Any]:
    with _db_conn(request) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, metric_name, COUNT(*) AS cnt, MAX(recorded_at) AS latest
                FROM biomarkers
                WHERE user_id = %s
                GROUP BY source, metric_name
                ORDER BY source, latest DESC
                """,
                (user_id,),
            )
            rows = [
                {
                    "source": r[0],
                    "metric_name": r[1],
                    "count": r[2],
                    "latest": r[3].isoformat() if r[3] else None,
                }
                for r in cur.fetchall()
            ]
    return {"user_id": user_id, "biomarkers": rows}
