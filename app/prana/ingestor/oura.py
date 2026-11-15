from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prana.ingestor.registry import resolve_canonical_metric
from prana.ingestor.storage import (
    IngestionIssueRecord,
    ParsedMetricPoint,
    bulk_upsert_biomarkers,
    insert_raw_ingestion,
    mark_raw_status,
    record_ingestion_issues,
)

IGNORED_OURA_EVENT_TYPES = {
    "debug_event",
    "debug_data",
    "ble_connection",
    "time_sync",
    "ring_start",
    "state_change",
    "feature_session",
    "wear_event",
    "scan_start",
    "scan_end",
    "motion_event",
    "motion_period",
    "sleep_acm_period",
    "alert_event",
    "rtc_beacon",
    "user_information",
}

KNOWN_OURA_KEYS = {
    "event_type",
    "type",
    "timestamp",
    "recorded_at",
    "time",
    "start_time",
    "created_at",
    "bpm",
    "heart_rate",
    "hr",
    "hr_bpm",
    "ibi_ms",
    "amplitude",
    "quality",
    "resting_heart_rate",
    "rhr",
    "rmssd",
    "rmssd_ms",
    "hrv",
    "interval_min",
    "spo2",
    "blood_oxygen",
    "temperature_delta",
    "skin_temperature_delta",
    "temperature",
    "temps_c",
    "sleep_duration",
    "total_sleep_hours",
    "duration_hours",
    "bedtime_start_ds",
    "bedtime_end_ds",
    "duration_sec",
    "total_sleep_sec",
    "steps",
    "met",
    "state",
    "value",
    "value_min",
    "value_max",
    "unit",
    "device",
    "serial",
    "tag",
}


def _resolve_tz(user_tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(user_tz)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_float(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return None


def _as_float_list(raw: Any) -> List[float]:
    if not isinstance(raw, list):
        return []
    return [v for item in raw if (v := _as_float(item)) is not None]


def _first_float(ev: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if ev.get(key) is not None:
            val = _as_float(ev.get(key))
            if val is not None:
                return val
    return None


def parse_oura_timestamp(raw: Any, user_tz: str = "UTC") -> datetime:
    """Parse Oura ISO-8601 string or unix epoch (sec/ms) into a tz-aware datetime."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        ts = float(raw) / 1000.0 if raw > 1e12 else float(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Unrecognized timestamp: {raw!r}")
    dt = datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_resolve_tz(user_tz))
    return dt


def _extract_event_timestamp(ev: Dict[str, Any], user_tz: str) -> datetime:
    for key in ("timestamp", "recorded_at", "time", "start_time", "created_at"):
        if ev.get(key) is not None:
            return parse_oura_timestamp(ev[key], user_tz=user_tz)
    raise ValueError("Missing timestamp field")


def _classify_oura_event(
    ev: Dict[str, Any],
) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[float], Optional[float]]:
    """Return (metric_name, value, unit, value_min, value_max) for an Oura event."""
    ev_type = str(ev.get("event_type") or ev.get("type") or "").lower().strip()

    rmssd_list = _as_float_list(ev.get("rmssd_ms"))
    hrv = _first_float(ev, "rmssd", "hrv")
    if rmssd_list or hrv is not None or ev_type in ("hrv", "hrv_rmssd", "hrv_event"):
        mean_hrv = (
            round(sum(rmssd_list) / len(rmssd_list), 4)
            if rmssd_list
            else (hrv if hrv is not None else _as_float(ev.get("value")))
        )
        return (
            "hrv_rmssd",
            mean_hrv,
            "ms",
            min(rmssd_list) if rmssd_list else None,
            max(rmssd_list) if rmssd_list else None,
        )

    hr_bpm = _as_float_list(ev.get("hr_bpm"))
    if hr_bpm or ev_type in ("green_ibi_quality_event", "ibi_and_amplitude_event"):
        mean_hr = round(sum(hr_bpm) / len(hr_bpm), 4) if hr_bpm else _as_float(ev.get("value"))
        return (
            "heart_rate",
            mean_hr,
            "bpm",
            min(hr_bpm) if hr_bpm else None,
            max(hr_bpm) if hr_bpm else None,
        )

    bpm = _first_float(ev, "bpm", "heart_rate", "hr")
    if bpm is not None or ev_type in ("heart_rate", "hr", "ibi"):
        return "heart_rate", (bpm if bpm is not None else _as_float(ev.get("value"))), "bpm", None, None

    rhr = _first_float(ev, "resting_heart_rate", "rhr")
    if rhr is not None or ev_type in ("resting_heart_rate", "rhr"):
        return "resting_heart_rate", (rhr if rhr is not None else _as_float(ev.get("value"))), "bpm", None, None

    spo2 = _first_float(ev, "spo2", "blood_oxygen")
    if spo2 is not None or ev_type == "spo2":
        return "spo2", (spo2 if spo2 is not None else _as_float(ev.get("value"))), "%", None, None

    temps_c = ev.get("temps_c")
    if isinstance(temps_c, list) or ev_type in ("temp_event", "sleep_temp_event", "temp_period"):
        nums = _as_float_list(temps_c)
        mean_temp = round(sum(nums) / len(nums), 4) if nums else _as_float(ev.get("value"))
        return (
            "skin_temperature",
            mean_temp,
            "degC",
            min(nums) if nums else None,
            max(nums) if nums else None,
        )

    temp = _first_float(ev, "temperature_delta", "skin_temperature_delta", "temperature")
    if temp is not None or "temp" in ev_type:
        return "skin_temperature_delta", (temp if temp is not None else _as_float(ev.get("value"))), "degC", None, None

    sleep_hr = _first_float(ev, "sleep_duration", "total_sleep_hours", "duration_hours")
    if sleep_hr is not None or ev_type in ("sleep", "sleep_duration", "sleep_period", "bedtime_period"):
        val = sleep_hr
        if val is None:
            sec = _first_float(ev, "duration_sec", "total_sleep_sec")
            val = round(sec / 3600.0, 4) if sec is not None else _as_float(ev.get("value"))
        return "sleep_duration", val, "hr", None, None

    steps = _as_float(ev.get("steps"))
    if steps is not None or ev_type in ("steps", "step_count"):
        return "steps", (steps if steps is not None else _as_float(ev.get("value"))), "count", None, None

    met = _as_float_list(ev.get("met"))
    if met or ev_type == "activity_information":
        mean_met = round(sum(met) / len(met), 4) if met else _as_float(ev.get("value"))
        return (
            "activity_met",
            mean_met,
            "MET",
            min(met) if met else None,
            max(met) if met else None,
        )

    return (ev_type or None), _as_float(ev.get("value")), ev.get("unit"), None, None


def parse_oura_payload(
    payload: Dict[str, Any],
    user_tz: str = "UTC",
) -> Tuple[List[ParsedMetricPoint], List[IngestionIssueRecord]]:
    """Parse raw open_oura payload into normalized points and contract issues."""
    points: List[ParsedMetricPoint] = []
    issues: List[IngestionIssueRecord] = []

    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        issues.append(
            IngestionIssueRecord(
                severity="sos",
                issue_type="payload_shape",
                message="Expected top-level object with 'events' list",
                details={"keys": list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__},
            )
        )
        return points, issues

    default_device = str(payload.get("device") or "Oura Ring 4").strip()
    unknown_metrics_seen: set[str] = set()
    unknown_keys_seen: set[Tuple[str, str]] = set()

    for ev in payload["events"]:
        if not isinstance(ev, dict):
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="payload_shape",
                    message="Oura event item is not an object",
                    details={"type": type(ev).__name__},
                )
            )
            continue

        ev_type = str(ev.get("event_type") or ev.get("type") or "").lower().strip()
        if ev_type in IGNORED_OURA_EVENT_TYPES:
            continue
        if ev_type in ("green_ibi_quality_event", "ibi_and_amplitude_event") and ev.get("hr_bpm") == []:
            continue
        if ev_type == "hrv_event" and ev.get("rmssd_ms") == []:
            continue
        if ev_type in ("temp_event", "sleep_temp_event", "temp_period") and ev.get("temps_c") == []:
            continue

        try:
            recorded_at = _extract_event_timestamp(ev, user_tz=user_tz)
        except ValueError as exc:
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="unparseable_timestamp",
                    metric_name=str(ev.get("event_type") or ev.get("type") or "") or None,
                    message=f"Unparseable Oura event timestamp: {exc}",
                    details={"keys": list(ev.keys())},
                )
            )
            continue

        metric_name, val, unit, derived_min, derived_max = _classify_oura_event(ev)
        val_min = _as_float(ev.get("value_min"))
        val_max = _as_float(ev.get("value_max"))
        if val_min is None:
            val_min = derived_min
        if val_max is None:
            val_max = derived_max

        if not metric_name or (val is None and val_min is None and val_max is None):
            issues.append(
                IngestionIssueRecord(
                    severity="sos",
                    issue_type="no_usable_value",
                    metric_name=metric_name,
                    message=f"No usable metric/value in Oura event '{metric_name or 'unknown'}'",
                    details={"keys": list(ev.keys())},
                )
            )
            continue

        resolved = resolve_canonical_metric("oura_ble", metric_name)
        canonical_name = resolved[0] if resolved else None
        if resolved is None and metric_name not in unknown_metrics_seen:
            unknown_metrics_seen.add(metric_name)
            issues.append(
                IngestionIssueRecord(
                    severity="warning",
                    issue_type="unknown_metric",
                    metric_name=metric_name,
                    message=f"Unmapped oura_ble metric '{metric_name}' (stored without canonical_metric)",
                    details={"unit": unit},
                )
            )

        for k in ev:
            lk = str(k).lower()
            if lk not in KNOWN_OURA_KEYS and (metric_name, lk) not in unknown_keys_seen:
                unknown_keys_seen.add((metric_name, lk))
                issues.append(
                    IngestionIssueRecord(
                        severity="warning",
                        issue_type="unknown_entry_keys",
                        metric_name=metric_name,
                        message=f"New Oura event field '{k}' observed on '{metric_name}'",
                        details={"field": k},
                    )
                )

        device = str(ev.get("device") or default_device).strip()
        points.append(
            ParsedMetricPoint(
                metric_name=metric_name,
                canonical_metric=canonical_name,
                recorded_at=recorded_at,
                value=val,
                value_min=val_min,
                value_max=val_max,
                unit=unit,
                device=device,
                metadata=ev,
            )
        )

    return points, issues


def normalize_and_store_oura(
    conn: Any,
    user_id: int,
    raw_ingestion_id: int,
    payload: Dict[str, Any],
    user_tz: str = "UTC",
) -> Tuple[int, int, int]:
    """Normalize an already-stored raw Oura payload. Returns (points, sos, warnings)."""
    points, issues = parse_oura_payload(payload, user_tz=user_tz)
    sos_count = sum(1 for i in issues if i.severity == "sos")
    warning_count = sum(1 for i in issues if i.severity == "warning")

    try:
        with conn.cursor() as cur:
            bulk_upsert_biomarkers(cur, user_id, "oura_ble", points)
            record_ingestion_issues(cur, user_id, raw_ingestion_id, "oura_ble", issues)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        err_issue = IngestionIssueRecord(
            severity="sos",
            issue_type="db_error",
            message=f"Failed to persist normalized oura_ble data: {exc}",
        )
        with conn.cursor() as cur:
            record_ingestion_issues(cur, user_id, raw_ingestion_id, "oura_ble", [err_issue])
        conn.commit()
        mark_raw_status(conn, raw_ingestion_id, "failed", str(exc))
        raise

    if sos_count > 0:
        mark_raw_status(conn, raw_ingestion_id, "failed", f"{sos_count} SOS issue(s) during parse")
    else:
        mark_raw_status(conn, raw_ingestion_id, "processed")

    return len(points), sos_count, warning_count


def store_oura_payload(
    conn: Any,
    user_id: int,
    payload: Dict[str, Any],
    user_tz: str = "UTC",
) -> Dict[str, Any]:
    """Persist raw Oura payload first (with SHA-256 dedupe), then normalize and record issues."""
    raw_id, is_dup = insert_raw_ingestion(conn, user_id, "oura_ble", payload)
    if is_dup or raw_id is None:
        return {
            "status": "duplicate",
            "points_processed": 0,
            "sos_count": 0,
            "warning_count": 0,
        }

    points_count, sos_count, warning_count = normalize_and_store_oura(
        conn, user_id, raw_id, payload, user_tz=user_tz
    )
    return {
        "status": "ok" if sos_count == 0 else "partial_sos",
        "raw_ingestion_id": raw_id,
        "points_processed": points_count,
        "sos_count": sos_count,
        "warning_count": warning_count,
    }
