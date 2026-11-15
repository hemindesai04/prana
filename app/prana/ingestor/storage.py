from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

from prana.db import compute_payload_sha256, sanitize_json_value
from prana.ingestor.registry import resolve_canonical_metric
from prana.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ParsedMetricPoint:
    metric_name: str
    recorded_at: datetime
    value: Optional[float]
    value_min: Optional[float]
    value_max: Optional[float]
    unit: Optional[str]
    device: str = ""
    canonical_metric: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedWorkout:
    workout_type: str
    started_at: datetime
    ended_at: Optional[datetime]
    duration_sec: Optional[float]
    calories: Optional[float]
    device: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestionIssueRecord:
    severity: str  # "sos" | "warning"
    issue_type: str
    message: str
    metric_name: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


def insert_raw_ingestion(
    conn: Any,
    user_id: int,
    source: str,
    payload: Dict[str, Any],
) -> Tuple[Optional[int], bool]:
    """Commit raw payload first. Returns (raw_id, is_duplicate)."""
    sanitized = sanitize_json_value(payload)
    sha = compute_payload_sha256(sanitized)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_ingestions (user_id, source, payload_sha256, status, payload)
            VALUES (%s, %s, %s, 'pending', %s)
            ON CONFLICT (user_id, source, payload_sha256) DO NOTHING
            RETURNING id
            """,
            (user_id, source, sha, json.dumps(sanitized)),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return None, True
    return int(row[0]), False


def bulk_upsert_biomarkers(
    cur: Any,
    user_id: int,
    source: str,
    points: List[ParsedMetricPoint],
) -> None:
    if not points:
        return
    rows = []
    for pt in points:
        canonical = pt.canonical_metric
        if canonical is None:
            resolved = resolve_canonical_metric(source, pt.metric_name)
            canonical = resolved[0] if resolved else None
        rows.append(
            (
                user_id,
                source,
                pt.device,
                pt.metric_name,
                canonical,
                pt.recorded_at,
                pt.value,
                pt.value_min,
                pt.value_max,
                pt.unit,
                json.dumps(sanitize_json_value(pt.metadata)),
            )
        )
    cur.executemany(
        """
        INSERT INTO biomarkers
            (user_id, source, device, metric_name, canonical_metric, recorded_at,
             value, value_min, value_max, unit, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, source, device, metric_name, recorded_at) DO UPDATE SET
            canonical_metric = COALESCE(EXCLUDED.canonical_metric, biomarkers.canonical_metric),
            value = COALESCE(EXCLUDED.value, biomarkers.value),
            value_min = COALESCE(EXCLUDED.value_min, biomarkers.value_min),
            value_max = COALESCE(EXCLUDED.value_max, biomarkers.value_max),
            unit = COALESCE(EXCLUDED.unit, biomarkers.unit),
            metadata = biomarkers.metadata || EXCLUDED.metadata
        """,
        rows,
    )


def bulk_upsert_workouts(
    cur: Any,
    user_id: int,
    source: str,
    workouts: List[ParsedWorkout],
) -> None:
    if not workouts:
        return
    rows = [
        (
            user_id,
            source,
            w.device,
            w.workout_type,
            w.started_at,
            w.ended_at,
            w.duration_sec,
            w.calories,
            json.dumps(sanitize_json_value(w.metadata)),
        )
        for w in workouts
    ]
    cur.executemany(
        """
        INSERT INTO workouts
            (user_id, source, device, workout_type, started_at, ended_at,
             duration_sec, calories, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, source, device, workout_type, started_at) DO UPDATE SET
            ended_at = COALESCE(EXCLUDED.ended_at, workouts.ended_at),
            duration_sec = COALESCE(EXCLUDED.duration_sec, workouts.duration_sec),
            calories = COALESCE(EXCLUDED.calories, workouts.calories),
            metadata = workouts.metadata || EXCLUDED.metadata
        """,
        rows,
    )


def record_ingestion_issues(
    cur: Any,
    user_id: Optional[int],
    raw_ingestion_id: Optional[int],
    source: str,
    issues: List[IngestionIssueRecord],
) -> None:
    if not issues:
        return
    rows = []
    for issue in issues:
        log_extra = {"user_id": user_id, "source": source}
        if issue.severity == "sos":
            logger.critical("SOS [%s] %s", issue.issue_type, issue.message, extra=log_extra)
        else:
            logger.warning("Drift [%s] %s", issue.issue_type, issue.message, extra=log_extra)
        rows.append(
            (
                user_id,
                raw_ingestion_id,
                source,
                issue.severity,
                issue.issue_type,
                issue.metric_name,
                issue.message,
                json.dumps(sanitize_json_value(issue.details)),
            )
        )
    cur.executemany(
        """
        INSERT INTO ingestion_issues
            (user_id, raw_ingestion_id, source, severity, issue_type, metric_name, message, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )


def mark_raw_status(
    conn: Any,
    raw_ingestion_id: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE raw_ingestions
            SET status = %s, processed_at = NOW(), error = %s
            WHERE id = %s
            """,
            (status, error, raw_ingestion_id),
        )
    conn.commit()
