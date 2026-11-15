from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from prana.ingestor.registry import resolve_canonical_metric
from prana.ingestor.storage import (
    IngestionIssueRecord,
    ParsedMetricPoint,
    bulk_upsert_biomarkers,
    insert_raw_ingestion,
    mark_raw_status,
    record_ingestion_issues,
)


class NormalizedPoint(BaseModel):
    metric_name: str
    recorded_at: datetime
    value: Optional[float] = None
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    unit: Optional[str] = None
    device: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GenericIngestPayload(BaseModel):
    source: str
    points: List[NormalizedPoint]
    raw: Optional[Dict[str, Any]] = None


def _localize(dt: datetime, user_tz: str) -> datetime:
    if dt.tzinfo is not None:
        return dt
    try:
        tz = ZoneInfo(user_tz)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return dt.replace(tzinfo=tz)


def store_generic_points(
    conn: Any,
    user_id: int,
    payload: GenericIngestPayload,
    user_tz: str = "UTC",
) -> Dict[str, Any]:
    """Persist raw payload first (with SHA-256 dedupe), then bulk upsert normalized points."""
    raw_doc = payload.raw if payload.raw is not None else payload.model_dump(mode="json")
    raw_id, is_dup = insert_raw_ingestion(conn, user_id, payload.source, raw_doc)
    if is_dup or raw_id is None:
        return {
            "status": "duplicate",
            "points_processed": 0,
            "warning_count": 0,
        }

    issues: List[IngestionIssueRecord] = []
    unknown_seen: set[str] = set()
    parsed: List[ParsedMetricPoint] = []
    for pt in payload.points:
        resolved = resolve_canonical_metric(payload.source, pt.metric_name)
        canonical = resolved[0] if resolved else None
        if resolved is None and pt.metric_name not in unknown_seen:
            unknown_seen.add(pt.metric_name)
            issues.append(
                IngestionIssueRecord(
                    severity="warning",
                    issue_type="unknown_metric",
                    metric_name=pt.metric_name,
                    message=f"Unmapped {payload.source} metric '{pt.metric_name}'",
                    details={"unit": pt.unit},
                )
            )
        parsed.append(
            ParsedMetricPoint(
                metric_name=pt.metric_name,
                canonical_metric=canonical,
                recorded_at=_localize(pt.recorded_at, user_tz),
                value=pt.value,
                value_min=pt.value_min,
                value_max=pt.value_max,
                unit=pt.unit,
                device=pt.device.strip(),
                metadata=pt.metadata,
            )
        )

    try:
        with conn.cursor() as cur:
            bulk_upsert_biomarkers(cur, user_id, payload.source, parsed)
            record_ingestion_issues(cur, user_id, raw_id, payload.source, issues)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        err_issue = IngestionIssueRecord(
            severity="sos",
            issue_type="db_error",
            message=f"Failed to persist normalized {payload.source} points: {exc}",
        )
        with conn.cursor() as cur:
            record_ingestion_issues(cur, user_id, raw_id, payload.source, [err_issue])
        conn.commit()
        mark_raw_status(conn, raw_id, "failed", str(exc))
        raise

    mark_raw_status(conn, raw_id, "processed")
    return {
        "status": "ok",
        "raw_ingestion_id": raw_id,
        "points_processed": len(parsed),
        "warning_count": len(issues),
    }
