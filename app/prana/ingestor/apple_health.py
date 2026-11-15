from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prana.ingestor.registry import resolve_canonical_metric
from prana.ingestor.storage import (
    IngestionIssueRecord,
    ParsedMetricPoint,
    ParsedWorkout,
    bulk_upsert_biomarkers,
    bulk_upsert_workouts,
    insert_raw_ingestion,
    mark_raw_status,
    record_ingestion_issues,
)
from prana.logging_setup import get_logger

logger = get_logger(__name__)

NUMERIC_KEYS = ("qty", "avg", "value", "totalsleep", "asleep")
EXCLUDED_EXTRA_KEYS = {"date", "qty", "avg", "min", "max", "value"}
KNOWN_ENTRY_KEYS = {
    "date",
    "qty",
    "avg",
    "min",
    "max",
    "value",
    "source",
    "units",
    "totalsleep",
    "asleep",
    "core",
    "deep",
    "rem",
    "awake",
    "inbed",
    "sleepstart",
    "sleepend",
    "start",
    "end",
    "startdate",
    "enddate",
    "context",
    "heartbeatseries",
}
KNOWN_WORKOUT_KEYS = {
    "name",
    "workoutactivitytype",
    "start",
    "end",
    "duration",
    "activeenergyburned",
    "basalenergyburned",
    "source",
    "distance",
    "heartratedata",
    "heartrate",
    "route",
    "elevation",
    "metadata",
    "location",
    "temperature",
    "humidity",
    "stepcount",
    "flightsclimbed",
    "swimmingstrokecount",
    "intensity",
    "speed",
    "pace",
}


def _resolve_tz(user_tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(user_tz)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def parse_timestamp(raw: str, user_tz: str = "UTC") -> datetime:
    """Parse ISO-8601 or Health Auto Export timestamp strings; localize naive ones."""
    cleaned = raw.strip()
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(f"Unrecognized timestamp format: {raw}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_resolve_tz(user_tz))
    return dt


def _as_float(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return None


def _lower_map(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k).lower(): v for k, v in entry.items()}


def _extract_device(raw_source: Any) -> str:
    """Extract device name from either a plain string or an HAE source dict."""
    if isinstance(raw_source, dict):
        return str(raw_source.get("name") or raw_source.get("identifier") or "").strip()
    return str(raw_source or "").strip()


def extract_numeric(entry: Dict[str, Any]) -> Optional[float]:
    """Extract primary numeric value case-insensitively from a metric entry."""
    lowered = _lower_map(entry)
    for key in NUMERIC_KEYS:
        num = _as_float(lowered.get(key))
        if num is not None:
            return num
    return None


def parse_apple_health_payload(
    payload: Dict[str, Any],
    user_tz: str = "UTC",
) -> Tuple[List[ParsedMetricPoint], List[ParsedWorkout], List[IngestionIssueRecord]]:
    """Parse Health Auto Export JSON into normalized metrics, workouts, and contract issues."""
    points: List[ParsedMetricPoint] = []
    workouts: List[ParsedWorkout] = []
    issues: List[IngestionIssueRecord] = []

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        issues.append(
            IngestionIssueRecord(
                severity="sos",
                issue_type="payload_shape",
                message="Expected top-level object with 'data' dict",
                details={"keys": list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__},
            )
        )
        return points, workouts, issues

    data = payload["data"]
    raw_metrics = data.get("metrics")
    if raw_metrics is not None and not isinstance(raw_metrics, list):
        issues.append(
            IngestionIssueRecord(
                severity="sos",
                issue_type="payload_shape",
                message="Expected 'data.metrics' to be a list",
                details={"type": type(raw_metrics).__name__},
            )
        )
        raw_metrics = []

    raw_workouts = data.get("workouts")
    if raw_workouts is not None and not isinstance(raw_workouts, list):
        issues.append(
            IngestionIssueRecord(
                severity="sos",
                issue_type="payload_shape",
                message="Expected 'data.workouts' to be a list",
                details={"type": type(raw_workouts).__name__},
            )
        )
        raw_workouts = []

    unknown_metrics_seen: set[str] = set()
    unknown_entry_keys_seen: set[Tuple[str, str]] = set()
    unknown_workout_keys_seen: set[str] = set()

    for metric in raw_metrics or []:
        if not isinstance(metric, dict):
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="payload_shape",
                    message="Metric item is not an object",
                    details={"type": type(metric).__name__},
                )
            )
            continue
        name = metric.get("name")
        unit = metric.get("units")
        if not name or not isinstance(name, str):
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="payload_shape",
                    message="Metric object missing string 'name'",
                    details={"keys": list(metric.keys())},
                )
            )
            continue

        resolved = resolve_canonical_metric("apple_health", name)
        canonical_name = resolved[0] if resolved else None
        if resolved is None and name not in unknown_metrics_seen:
            unknown_metrics_seen.add(name)
            issues.append(
                IngestionIssueRecord(
                    severity="warning",
                    issue_type="unknown_metric",
                    metric_name=name,
                    message=f"Unmapped apple_health metric '{name}' (stored without canonical_metric)",
                    details={"units": unit},
                )
            )

        entries = metric.get("data")
        if entries is not None and not isinstance(entries, list):
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="payload_shape",
                    metric_name=name,
                    message=f"Expected 'data' list for metric '{name}'",
                )
            )
            continue

        for entry in entries or []:
            if not isinstance(entry, dict):
                issues.append(
                    IngestionIssueRecord(
                        severity="sos",
                        issue_type="payload_shape",
                        metric_name=name,
                        message=f"Entry for metric '{name}' is not an object",
                    )
                )
                continue

            lowered = _lower_map(entry)
            raw_date = lowered.get("date")
            if not raw_date:
                issues.append(
                    IngestionIssueRecord(
                        severity="sos",
                        issue_type="unparseable_timestamp",
                        metric_name=name,
                        message=f"Missing 'date' in metric '{name}' entry",
                        details={"keys": list(entry.keys())},
                    )
                )
                continue
            try:
                recorded_at = parse_timestamp(str(raw_date), user_tz=user_tz)
            except ValueError:
                issues.append(
                    IngestionIssueRecord(
                        severity="sos",
                        issue_type="unparseable_timestamp",
                        metric_name=name,
                        message=f"Unparseable date {raw_date!r} for metric '{name}'",
                        details={"raw_date": str(raw_date)},
                    )
                )
                continue

            val = extract_numeric(entry)
            val_min = _as_float(lowered.get("min"))
            val_max = _as_float(lowered.get("max"))
            if val is None and val_min is None and val_max is None:
                issues.append(
                    IngestionIssueRecord(
                        severity="sos",
                        issue_type="no_usable_value",
                        metric_name=name,
                        message=f"No numeric value found in metric '{name}' entry",
                        details={"keys": list(entry.keys())},
                    )
                )
                continue

            for k in lowered:
                if k not in KNOWN_ENTRY_KEYS and (name, k) not in unknown_entry_keys_seen:
                    unknown_entry_keys_seen.add((name, k))
                    issues.append(
                        IngestionIssueRecord(
                            severity="warning",
                            issue_type="unknown_entry_keys",
                            metric_name=name,
                            message=f"New entry field '{k}' observed on metric '{name}'",
                            details={"field": k},
                        )
                    )

            device = _extract_device(lowered.get("source"))
            extra = {k: v for k, v in entry.items() if str(k).lower() not in EXCLUDED_EXTRA_KEYS}
            points.append(
                ParsedMetricPoint(
                    metric_name=name,
                    canonical_metric=canonical_name,
                    recorded_at=recorded_at,
                    value=val,
                    value_min=val_min,
                    value_max=val_max,
                    unit=unit,
                    device=device,
                    metadata=extra,
                )
            )

    for w in raw_workouts or []:
        if not isinstance(w, dict):
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="payload_shape",
                    message="Workout item is not an object",
                )
            )
            continue
        w_type = w.get("name") or w.get("workoutActivityType") or "unknown"
        raw_start = w.get("start")
        if not raw_start:
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="unparseable_timestamp",
                    message=f"Missing 'start' in workout '{w_type}'",
                )
            )
            continue
        try:
            started_at = parse_timestamp(str(raw_start), user_tz=user_tz)
            ended_at = parse_timestamp(str(w["end"]), user_tz=user_tz) if w.get("end") else None
        except ValueError:
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="unparseable_timestamp",
                    message=f"Unparseable workout start/end for '{w_type}'",
                    details={"start": str(raw_start), "end": str(w.get("end"))},
                )
            )
            continue

        for k in w:
            lk = str(k).lower()
            if lk not in KNOWN_WORKOUT_KEYS and lk not in unknown_workout_keys_seen:
                unknown_workout_keys_seen.add(lk)
                issues.append(
                    IngestionIssueRecord(
                        severity="warning",
                        issue_type="unknown_workout_keys",
                        message=f"New workout field '{k}' observed on '{w_type}'",
                        details={"field": k},
                    )
                )

        energy = w.get("activeEnergyBurned")
        calories = _as_float(energy.get("qty")) if isinstance(energy, dict) else _as_float(energy)
        device = _extract_device(w.get("source"))
        workouts.append(
            ParsedWorkout(
                workout_type=str(w_type),
                started_at=started_at,
                ended_at=ended_at,
                duration_sec=_as_float(w.get("duration")),
                calories=calories,
                device=device,
                metadata=w,
            )
        )

    return points, workouts, issues


def normalize_and_store_apple_health(
    conn: Any,
    user_id: int,
    raw_ingestion_id: int,
    payload: Dict[str, Any],
    user_tz: str = "UTC",
) -> Tuple[int, int, int, int]:
    """Normalize an already-stored raw Apple Health payload. Returns (metrics, workouts, sos, warnings)."""
    points, workouts, issues = parse_apple_health_payload(payload, user_tz=user_tz)
    sos_count = sum(1 for i in issues if i.severity == "sos")
    warning_count = sum(1 for i in issues if i.severity == "warning")

    try:
        with conn.cursor() as cur:
            bulk_upsert_biomarkers(cur, user_id, "apple_health", points)
            bulk_upsert_workouts(cur, user_id, "apple_health", workouts)
            record_ingestion_issues(cur, user_id, raw_ingestion_id, "apple_health", issues)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        err_issue = IngestionIssueRecord(
            severity="sos",
            issue_type="db_error",
            message=f"Failed to persist normalized apple_health data: {exc}",
        )
        with conn.cursor() as cur:
            record_ingestion_issues(cur, user_id, raw_ingestion_id, "apple_health", [err_issue])
        conn.commit()
        mark_raw_status(conn, raw_ingestion_id, "failed", str(exc))
        raise

    if sos_count > 0:
        mark_raw_status(conn, raw_ingestion_id, "failed", f"{sos_count} SOS issue(s) during parse")
    else:
        mark_raw_status(conn, raw_ingestion_id, "processed")

    return len(points), len(workouts), sos_count, warning_count


def store_apple_health_payload(
    conn: Any,
    user_id: int,
    payload: Dict[str, Any],
    user_tz: str = "UTC",
) -> Dict[str, Any]:
    """Persist raw payload first (with SHA-256 dedupe), then normalize and record issues."""
    raw_id, is_dup = insert_raw_ingestion(conn, user_id, "apple_health", payload)
    if is_dup or raw_id is None:
        return {
            "status": "duplicate",
            "metrics_processed": 0,
            "workouts_processed": 0,
            "sos_count": 0,
            "warning_count": 0,
        }

    metrics_count, workouts_count, sos_count, warning_count = normalize_and_store_apple_health(
        conn, user_id, raw_id, payload, user_tz=user_tz
    )
    return {
        "status": "ok" if sos_count == 0 else "partial_sos",
        "raw_ingestion_id": raw_id,
        "metrics_processed": metrics_count,
        "workouts_processed": workouts_count,
        "sos_count": sos_count,
        "warning_count": warning_count,
    }
