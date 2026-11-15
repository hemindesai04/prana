import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from prana.logging_setup import get_logger

logger = get_logger(__name__)

NUMERIC_KEYS = ("qty", "avg", "value", "totalSleep", "asleep")
EXCLUDED_EXTRA_KEYS = {"date", "qty", "avg", "min", "max"}


@dataclass(frozen=True)
class ParsedMetricPoint:
    metric_name: str
    recorded_at: datetime
    value: Optional[float]
    value_min: Optional[float]
    value_max: Optional[float]
    unit: Optional[str]
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class ParsedWorkout:
    workout_type: str
    started_at: datetime
    ended_at: Optional[datetime]
    duration_sec: Optional[float]
    calories: Optional[float]
    metadata: Dict[str, Any]


def parse_timestamp(raw: str) -> datetime:
    """Parse ISO-8601 or Health Auto Export timestamp strings."""
    cleaned = raw.strip()
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized timestamp format: {raw}")


def extract_numeric(entry: Dict[str, Any]) -> Optional[float]:
    """Extract primary numeric value from a Health Auto Export metric entry."""
    for key in NUMERIC_KEYS:
        val = entry.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def _as_float(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return None


def parse_apple_health_payload(
    payload: Dict[str, Any],
) -> Tuple[List[ParsedMetricPoint], List[ParsedWorkout]]:
    """Parse a Health Auto Export JSON payload into normalized metrics and workouts."""
    data = payload.get("data") or {}
    points: List[ParsedMetricPoint] = []
    workouts: List[ParsedWorkout] = []

    for metric in data.get("metrics") or []:
        name = metric.get("name")
        unit = metric.get("units")
        if not name:
            continue
        for entry in metric.get("data") or []:
            raw_date = entry.get("date")
            if not raw_date:
                continue
            try:
                recorded_at = parse_timestamp(str(raw_date))
            except ValueError:
                logger.warning("Skipping unparseable date %r for metric %s", raw_date, name)
                continue
            extra = {k: v for k, v in entry.items() if k not in EXCLUDED_EXTRA_KEYS}
            points.append(
                ParsedMetricPoint(
                    metric_name=name,
                    recorded_at=recorded_at,
                    value=extract_numeric(entry),
                    value_min=_as_float(entry.get("min")),
                    value_max=_as_float(entry.get("max")),
                    unit=unit,
                    metadata=extra,
                )
            )

    for w in data.get("workouts") or []:
        w_type = w.get("name") or w.get("workoutActivityType") or "unknown"
        raw_start = w.get("start")
        if not raw_start:
            continue
        try:
            started_at = parse_timestamp(str(raw_start))
            ended_at = parse_timestamp(str(w["end"])) if w.get("end") else None
        except ValueError:
            logger.warning("Skipping unparseable workout start/end for %s", w_type)
            continue

        energy = w.get("activeEnergyBurned")
        calories = _as_float(energy.get("qty")) if isinstance(energy, dict) else _as_float(energy)
        workouts.append(
            ParsedWorkout(
                workout_type=str(w_type),
                started_at=started_at,
                ended_at=ended_at,
                duration_sec=_as_float(w.get("duration")),
                calories=calories,
                metadata=w,
            )
        )

    return points, workouts


def store_apple_health_payload(
    conn: Any,
    user_id: int,
    payload: Dict[str, Any],
) -> Tuple[int, int]:
    """Persist raw payload and upsert parsed Apple Health metrics and workouts."""
    points, workouts = parse_apple_health_payload(payload)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_ingestions (user_id, source, payload) VALUES (%s, %s, %s)",
            (user_id, "apple_health", json.dumps(payload)),
        )
        for pt in points:
            cur.execute(
                """
                INSERT INTO biomarkers
                    (user_id, source, metric_name, recorded_at, value, value_min, value_max, unit, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, source, metric_name, recorded_at) DO UPDATE SET
                    value = EXCLUDED.value,
                    value_min = COALESCE(EXCLUDED.value_min, biomarkers.value_min),
                    value_max = COALESCE(EXCLUDED.value_max, biomarkers.value_max),
                    unit = COALESCE(EXCLUDED.unit, biomarkers.unit),
                    metadata = biomarkers.metadata || EXCLUDED.metadata
                """,
                (
                    user_id,
                    "apple_health",
                    pt.metric_name,
                    pt.recorded_at,
                    pt.value,
                    pt.value_min,
                    pt.value_max,
                    pt.unit,
                    json.dumps(pt.metadata),
                ),
            )
        for w in workouts:
            cur.execute(
                """
                INSERT INTO workouts
                    (user_id, source, workout_type, started_at, ended_at, duration_sec, calories, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, source, workout_type, started_at) DO UPDATE SET
                    ended_at = COALESCE(EXCLUDED.ended_at, workouts.ended_at),
                    duration_sec = COALESCE(EXCLUDED.duration_sec, workouts.duration_sec),
                    calories = COALESCE(EXCLUDED.calories, workouts.calories),
                    metadata = workouts.metadata || EXCLUDED.metadata
                """,
                (
                    user_id,
                    "apple_health",
                    w.workout_type,
                    w.started_at,
                    w.ended_at,
                    w.duration_sec,
                    w.calories,
                    json.dumps(w.metadata),
                ),
            )
    conn.commit()
    return len(points), len(workouts)
