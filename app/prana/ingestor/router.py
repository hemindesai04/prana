from datetime import datetime, timezone
import hmac
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from prana.db import hash_token, resolve_user
from prana.ingestor.apple_health import (
    normalize_and_store_apple_health,
    store_apple_health_payload,
)
from prana.ingestor.points import GenericIngestPayload, store_generic_points
from prana.logging_setup import get_logger
from prana.migrations import get_migration_status

logger = get_logger(__name__)
router = APIRouter()


class CreateUserRequest(BaseModel):
    slug: str
    display_name: str
    ingest_token: str
    timezone: str = "UTC"


class ReprocessRequest(BaseModel):
    raw_ingestion_id: Optional[int] = None
    status: str = "failed"
    limit: int = 50


def get_db_conn(request: Request) -> Iterator[Any]:
    """Yield a single pooled (or test-injected) DB connection for the entire request."""
    override = getattr(request.app.state, "conn_factory", None)
    if override is not None:
        with override() as conn:
            yield conn
        return
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool not initialized")
    with pool.connection() as conn:
        yield conn


def require_user(
    conn: Any = Depends(get_db_conn),
    x_ingest_token: Optional[str] = Header(default=None),
) -> Tuple[int, str]:
    """Resolve (user_id, timezone) from X-Ingest-Token or raise 401."""
    if not x_ingest_token:
        raise HTTPException(status_code=401, detail="Missing X-Ingest-Token")
    user = resolve_user(conn, x_ingest_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid X-Ingest-Token")
    return user


def require_admin(
    request: Request,
    x_admin_token: Optional[str] = Header(default=None),
) -> None:
    """Validate X-Admin-Token in constant time."""
    configured = request.app.state.settings.admin_token
    if not configured or not x_admin_token or not hmac.compare_digest(x_admin_token, configured):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")


@router.get("/health")
def health(request: Request) -> JSONResponse:
    """Cheap liveness check: 200 if app & DB are reachable, 503 otherwise."""
    try:
        override = getattr(request.app.state, "conn_factory", None)
        if override is not None:
            with override() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        else:
            pool = getattr(request.app.state, "pool", None)
            if pool is None:
                return JSONResponse(status_code=503, content={"status": "unavailable"})
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/api/v1/status")
def system_status(
    request: Request,
    detail: int = Query(default=0),
    x_admin_token: Optional[str] = Header(default=None),
    conn: Any = Depends(get_db_conn),
) -> Dict[str, Any]:
    """Full component health and SOS report for monitoring and future notifications."""
    if detail:
        require_admin(request, x_admin_token)

    cfg = request.app.state.settings
    overall = "ok"

    def _elevate(level: str) -> None:
        nonlocal overall
        if level == "critical":
            overall = "critical"
        elif level == "degraded" and overall == "ok":
            overall = "degraded"

    # 1. DB & Migrations
    db_status = "ok"
    current_rev, head_rev, at_head = get_migration_status(conn, cfg.database_url)
    migrations_status = "ok" if at_head else "critical"
    if migrations_status == "critical":
        _elevate("critical")

    # 2. Ingestion & Issues
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT severity, COUNT(*)
            FROM ingestion_issues
            WHERE resolved_at IS NULL
            GROUP BY severity
            """
        )
        issue_counts = {str(r[0]): int(r[1]) for r in cur.fetchall()}

        cur.execute(
            """
            SELECT source, MAX(received_at)
            FROM raw_ingestions
            GROUP BY source
            ORDER BY source
            """
        )
        last_received = {
            str(r[0]): (r[1].isoformat() if r[1] else None) for r in cur.fetchall()
        }

        cur.execute(
            "SELECT COUNT(*) FROM raw_ingestions WHERE status = 'failed'"
        )
        failed_raw = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT status, started_at, finished_at, error
            FROM backup_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        last_backup_row = cur.fetchone()

    open_sos = issue_counts.get("sos", 0)
    open_warnings = issue_counts.get("warning", 0)
    ingestion_status = "ok"
    if open_sos > 0:
        ingestion_status = "critical"
        _elevate("critical")
    elif open_warnings > 0:
        ingestion_status = "degraded"
        _elevate("degraded")

    processing_status = "ok" if failed_raw == 0 else "critical"
    if processing_status == "critical":
        _elevate("critical")

    # 3. Backups
    backup_info: Dict[str, Any] = {"status": "degraded", "last_run": None, "last_error": None}
    if last_backup_row is not None:
        b_status, b_started, b_finished, b_error = last_backup_row
        ref_time = b_finished or b_started
        age_hours = (
            (datetime.now(timezone.utc) - ref_time).total_seconds() / 3600.0
            if ref_time is not None
            else 999.0
        )
        if b_status == "failed" or age_hours > 26.0:
            backup_info["status"] = "critical"
            _elevate("critical")
        else:
            backup_info["status"] = "ok"
        backup_info["last_run"] = ref_time.isoformat() if ref_time else None
        backup_info["age_hours"] = round(age_hours, 2)
        backup_info["last_error"] = b_error
    else:
        _elevate("degraded")

    # 4. Scheduler
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_running = bool(scheduler and scheduler.running)
    scheduler_status = "ok" if scheduler_running else "degraded"

    report: Dict[str, Any] = {
        "status": overall,
        "components": {
            "db": {"status": db_status},
            "migrations": {
                "status": migrations_status,
                "current": current_rev,
                "head": head_rev,
            },
            "ingestion": {
                "status": ingestion_status,
                "open_sos": open_sos,
                "open_warnings": open_warnings,
                "last_received_by_source": last_received,
            },
            "processing": {
                "status": processing_status,
                "failed_raw_ingestions": failed_raw,
            },
            "backups": backup_info,
            "scheduler": {"status": scheduler_status, "running": scheduler_running},
        },
    }

    if detail:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, raw_ingestion_id, source, severity, issue_type, metric_name, message, created_at
                FROM ingestion_issues
                WHERE resolved_at IS NULL
                ORDER BY created_at DESC
                LIMIT 25
                """
            )
            report["open_issues"] = [
                {
                    "id": r[0],
                    "user_id": r[1],
                    "raw_ingestion_id": r[2],
                    "source": r[3],
                    "severity": r[4],
                    "issue_type": r[5],
                    "metric_name": r[6],
                    "message": r[7],
                    "created_at": r[8].isoformat() if r[8] else None,
                }
                for r in cur.fetchall()
            ]
    return report


@router.post("/api/v1/users", dependencies=[Depends(require_admin)])
def create_user(
    body: CreateUserRequest,
    conn: Any = Depends(get_db_conn),
) -> Dict[str, Any]:
    """Create or update a household user and their ingest token."""
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
    conn: Any = Depends(get_db_conn),
    user: Tuple[int, str] = Depends(require_user),
) -> Dict[str, Any]:
    user_id, user_tz = user
    result = store_apple_health_payload(conn, user_id, payload, user_tz=user_tz)
    logger.info(
        "Ingested apple_health status=%s metrics=%d workouts=%d sos=%d warnings=%d",
        result["status"],
        result["metrics_processed"],
        result["workouts_processed"],
        result["sos_count"],
        result["warning_count"],
        extra={"user_id": user_id, "source": "apple_health"},
    )
    return {"user_id": user_id, **result}


@router.post("/api/v1/ingest/points")
def ingest_points(
    payload: GenericIngestPayload,
    conn: Any = Depends(get_db_conn),
    user: Tuple[int, str] = Depends(require_user),
) -> Dict[str, Any]:
    user_id, user_tz = user
    result = store_generic_points(conn, user_id, payload, user_tz=user_tz)
    logger.info(
        "Ingested generic status=%s points=%d warnings=%d",
        result["status"],
        result["points_processed"],
        result["warning_count"],
        extra={"user_id": user_id, "source": payload.source},
    )
    return {"user_id": user_id, "source": payload.source, **result}


@router.post("/api/v1/admin/reprocess", dependencies=[Depends(require_admin)])
def reprocess_raw_ingestions(
    body: ReprocessRequest,
    conn: Any = Depends(get_db_conn),
) -> Dict[str, Any]:
    """Re-run normalization on stored raw_ingestions after a parser update."""
    with conn.cursor() as cur:
        if body.raw_ingestion_id is not None:
            cur.execute(
                """
                SELECT r.id, r.user_id, r.source, r.payload, u.timezone
                FROM raw_ingestions r
                JOIN users u ON u.id = r.user_id
                WHERE r.id = %s
                """,
                (body.raw_ingestion_id,),
            )
        else:
            cur.execute(
                """
                SELECT r.id, r.user_id, r.source, r.payload, u.timezone
                FROM raw_ingestions r
                JOIN users u ON u.id = r.user_id
                WHERE r.status = %s
                ORDER BY r.received_at ASC
                LIMIT %s
                """,
                (body.status, body.limit),
            )
        rows = cur.fetchall()

    reprocessed: List[Dict[str, Any]] = []
    for raw_id, uid, source, payload, user_tz in rows:
        if source != "apple_health":
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ingestion_issues SET resolved_at = NOW() WHERE raw_ingestion_id = %s AND resolved_at IS NULL",
                (raw_id,),
            )
        conn.commit()
        metrics_c, workouts_c, sos_c, warn_c = normalize_and_store_apple_health(
            conn, int(uid), int(raw_id), payload, user_tz=str(user_tz)
        )
        reprocessed.append(
            {
                "raw_ingestion_id": raw_id,
                "metrics_processed": metrics_c,
                "workouts_processed": workouts_c,
                "sos_count": sos_c,
                "warning_count": warn_c,
            }
        )
    return {"status": "ok", "reprocessed_count": len(reprocessed), "results": reprocessed}


@router.get("/api/v1/stats")
def get_stats(
    source: Optional[str] = Query(default=None),
    device: Optional[str] = Query(default=None),
    conn: Any = Depends(get_db_conn),
    user: Tuple[int, str] = Depends(require_user),
) -> Dict[str, Any]:
    user_id, _ = user
    clauses = ["user_id = %s"]
    params: List[Any] = [user_id]
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    if device is not None:
        clauses.append("device = %s")
        params.append(device)
    where_sql = " AND ".join(clauses)

    raw_clauses = ["user_id = %s"]
    raw_params: List[Any] = [user_id]
    if source is not None:
        raw_clauses.append("source = %s")
        raw_params.append(source)
    raw_where = " AND ".join(raw_clauses)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT source, device, metric_name, canonical_metric, COUNT(*) AS cnt, MAX(recorded_at) AS latest
            FROM biomarkers
            WHERE {where_sql}
            GROUP BY source, device, metric_name, canonical_metric
            ORDER BY source, device, latest DESC
            """,
            tuple(params),
        )
        biomarkers = [
            {
                "source": r[0],
                "device": r[1],
                "metric_name": r[2],
                "canonical_metric": r[3],
                "count": r[4],
                "latest": r[5].isoformat() if r[5] else None,
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            f"""
            SELECT source, device, workout_type, COUNT(*) AS cnt, MAX(started_at) AS latest
            FROM workouts
            WHERE {where_sql}
            GROUP BY source, device, workout_type
            ORDER BY source, device, latest DESC
            """,
            tuple(params),
        )
        workouts = [
            {
                "source": r[0],
                "device": r[1],
                "workout_type": r[2],
                "count": r[3],
                "latest": r[4].isoformat() if r[4] else None,
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            f"""
            SELECT source, status, COUNT(*) AS cnt, MAX(received_at) AS last_received
            FROM raw_ingestions
            WHERE {raw_where}
            GROUP BY source, status
            ORDER BY source, status
            """,
            tuple(raw_params),
        )
        raw_ingestions = [
            {
                "source": r[0],
                "status": r[1],
                "count": r[2],
                "last_received_at": r[3].isoformat() if r[3] else None,
            }
            for r in cur.fetchall()
        ]

    return {
        "user_id": user_id,
        "biomarkers": biomarkers,
        "workouts": workouts,
        "raw_ingestions": raw_ingestions,
    }
