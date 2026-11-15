import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class NormalizedPoint(BaseModel):
    metric_name: str
    recorded_at: datetime
    value: Optional[float] = None
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    unit: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GenericIngestPayload(BaseModel):
    source: str
    points: List[NormalizedPoint]
    raw: Optional[Dict[str, Any]] = None


def store_generic_points(conn: Any, user_id: int, payload: GenericIngestPayload) -> int:
    """Persist optional raw payload and upsert normalized points for any wearable source."""
    with conn.cursor() as cur:
        if payload.raw is not None:
            cur.execute(
                "INSERT INTO raw_ingestions (user_id, source, payload) VALUES (%s, %s, %s)",
                (user_id, payload.source, json.dumps(payload.raw)),
            )
        for pt in payload.points:
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
                    payload.source,
                    pt.metric_name,
                    pt.recorded_at,
                    pt.value,
                    pt.value_min,
                    pt.value_max,
                    pt.unit,
                    json.dumps(pt.metadata),
                ),
            )
    conn.commit()
    return len(payload.points)
