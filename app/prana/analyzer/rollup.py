import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prana.logging_setup import get_logger

logger = get_logger(__name__)

# Metrics where intraday points represent increments/deltas summed per local day.
ADDITIVE_METRICS: Set[str] = {
    "steps",
    "active_calories",
    "basal_calories",
    "exercise_minutes",
    "stand_minutes",
}

# Metrics where intraday points represent levels/rates summarized as daily mean/min/max.
LEVEL_METRICS: Set[str] = {
    "heart_rate",
    "resting_heart_rate",
    "walking_heart_rate",
    "hrv_sdnn",
    "hrv_rmssd",
    "respiratory_rate",
    "vo2_max",
    "spo2",
    "skin_temperature_delta",
    "skin_temperature",
    "body_weight",
    "activity_met",
}

BASELINE_LOOKBACK_DAYS = 60
MIN_BASELINE_SAMPLES = 7
SLEEP_DEBT_WINDOW_DAYS = 14
WORKOUT_CHRONIC_DAYS = 28
WORKOUT_ACUTE_DAYS = 7


def resolve_user_tz(user_tz: Optional[str]) -> ZoneInfo:
    """Resolve user IANA timezone with UTC fallback."""
    try:
        return ZoneInfo((user_tz or "UTC").strip() or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _is_awake_segment(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    for key in ("value", "stage", "sleep_state", "state", "sleepState"):
        val = metadata.get(key)
        if isinstance(val, str) and val.strip().lower() in {"awake", "in_awake"}:
            return True
    return False


def _is_inbed_segment(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    for key in ("value", "stage", "sleep_state", "state", "sleepState"):
        val = metadata.get(key)
        if isinstance(val, str) and val.strip().lower().replace("_", "").replace(" ", "") == "inbed":
            return True
    return False


def compute_sleep_night_hours(
    segments: Sequence[Tuple[datetime, float, Dict[str, Any]]],
) -> Tuple[float, Dict[str, int]]:
    """Deduplicate overlapping sleep stage intervals and remove overarching InBed containers.

    Each input tuple is (start_dt, duration_hours, metadata). Returns (total_hours, stats).
    """
    raw_intervals: List[Tuple[float, float, bool]] = []
    seen_exact: Set[Tuple[int, int]] = set()

    for start_dt, dur_hr, meta in segments:
        if dur_hr is None or dur_hr <= 0.0 or dur_hr > 18.0:
            continue
        if _is_awake_segment(meta):
            continue
        start_s = start_dt.timestamp()
        end_s = start_s + dur_hr * 3600.0
        exact_key = (int(round(start_s)), int(round(end_s)))
        if exact_key in seen_exact:
            continue
        seen_exact.add(exact_key)
        raw_intervals.append((start_s, end_s, _is_inbed_segment(meta)))

    if not raw_intervals:
        return 0.0, {"raw_segments": 0, "dropped_containers": 0, "merged_segments": 0}

    # Drop overarching container intervals (>3h or InBed) wrapping >=2 children or a single dominant child.
    is_container = [False] * len(raw_intervals)
    for i, (s_i, e_i, inbed_i) in enumerate(raw_intervals):
        dur_i = e_i - s_i
        if dur_i <= 3.0 * 3600.0 and not inbed_i:
            continue
        wrapped_children = 0
        wrapped_non_inbed = False
        max_child_coverage = 0.0
        for j, (s_j, e_j, inbed_j) in enumerate(raw_intervals):
            if i == j:
                continue
            dur_j = e_j - s_j
            if dur_j <= 0.0 or dur_j >= dur_i:
                continue
            if not inbed_i and dur_j >= dur_i * 0.80:
                continue
            overlap = min(e_i, e_j) - max(s_i, s_j)
            if overlap / dur_j >= 0.80:
                wrapped_children += 1
                if not inbed_j:
                    wrapped_non_inbed = True
                max_child_coverage = max(max_child_coverage, overlap / dur_i)
                if wrapped_children >= 2:
                    is_container[i] = True
                    break
        if (
            not is_container[i]
            and wrapped_children == 1
            and ((inbed_i and wrapped_non_inbed and max_child_coverage >= 0.50) or max_child_coverage >= 0.75)
        ):
            is_container[i] = True

    kept = [(s, e) for idx, (s, e, _inbed) in enumerate(raw_intervals) if not is_container[idx]]
    dropped_count = sum(1 for flag in is_container if flag)
    if not kept:
        kept = [(s, e) for s, e, _inbed in raw_intervals]
        dropped_count = 0

    kept.sort(key=lambda iv: (iv[0], -iv[1]))
    merged: List[List[float]] = []
    for s, e in kept:
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        else:
            if e > merged[-1][1]:
                merged[-1][1] = e

    total_hours = sum(e - s for s, e in merged) / 3600.0
    if total_hours <= 0.0 or total_hours > 20.0:
        return 0.0, {
            "raw_segments": len(raw_intervals),
            "dropped_containers": dropped_count,
            "merged_segments": len(merged),
        }

    return round(total_hours, 4), {
        "raw_segments": len(raw_intervals),
        "dropped_containers": dropped_count,
        "merged_segments": len(merged),
    }


def _estimate_workout_load(duration_sec: Optional[float], calories: Optional[float]) -> float:
    """Estimate session training load units from duration and calories (unit-invariant for ACWR)."""
    dur_min = (float(duration_sec) / 60.0) if (duration_sec and duration_sec > 0) else 0.0
    cal = float(calories) if (calories and calories > 0) else 0.0
    if dur_min <= 0.0 and cal <= 0.0:
        return 0.0
    if dur_min <= 0.0:
        return round(cal * 0.2, 2)
    intensity = 1.0
    if cal > 0.0:
        kcal_per_min = cal / dur_min
        if kcal_per_min >= 10.0:
            intensity = 1.5
        elif kcal_per_min >= 6.0:
            intensity = 1.2
        elif kcal_per_min < 3.0:
            intensity = 0.7
    return round(dur_min * intensity, 2)


def _select_night_sleep_hours(device_hours: Sequence[Tuple[str, str, float]]) -> Optional[float]:
    """Select/combine nightly sleep hours across devices without zero-filling missing data."""
    valid = [(src, dev, hr) for src, dev, hr in device_hours if hr > 0.0]
    if not valid:
        return None
    plausible = [hr for _, _, hr in valid if hr >= 2.0]
    if len(plausible) >= 2:
        return round(statistics.mean(plausible), 4)
    if len(plausible) == 1:
        return round(plausible[0], 4)
    return round(max(hr for _, _, hr in valid), 4)


def _normalize_temperature_point(
    canonical: str,
    f_val: float,
    val_min: Optional[float],
    val_max: Optional[float],
    unit: Optional[str],
) -> Tuple[str, float, Optional[float], Optional[float], Optional[str]]:
    """Convert Fahrenheit or absolute skin/wrist temps mislabeled as delta into Celsius skin_temperature."""
    norm_unit = (unit or "").strip().lower()
    if norm_unit in {"degf", "°f", "f"} or (canonical == "skin_temperature_delta" and f_val > 50.0):
        c_val = (f_val - 32.0) * 5.0 / 9.0
        c_min = ((float(val_min) - 32.0) * 5.0 / 9.0) if val_min is not None else None
        c_max = ((float(val_max) - 32.0) * 5.0 / 9.0) if val_max is not None else None
        target_canon = "skin_temperature" if canonical == "skin_temperature_delta" and c_val > 15.0 else canonical
        return target_canon, c_val, c_min, c_max, "degC"
    if canonical == "skin_temperature_delta" and 15.0 < f_val <= 45.0:
        return "skin_temperature", f_val, val_min, val_max, unit or "degC"
    return canonical, f_val, val_min, val_max, unit


def _apply_rolling_baselines(rows: List[Dict[str, Any]]) -> None:
    """Populate baseline_mean, baseline_sd, z_score in-place strictly skipping missing days."""
    by_series: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (row["canonical_metric"], row["source"], row["device"])
        by_series[key].append(row)

    for series_rows in by_series.values():
        series_rows.sort(key=lambda r: r["day"])
        for idx, row in enumerate(series_rows):
            cur_day: date = row["day"]
            cutoff_day = cur_day - timedelta(days=BASELINE_LOOKBACK_DAYS)
            prior_vals = [
                series_rows[j]["value"]
                for j in range(idx)
                if cutoff_day <= series_rows[j]["day"] < cur_day
            ]
            if len(prior_vals) < MIN_BASELINE_SAMPLES:
                row["baseline_mean"] = None
                row["baseline_sd"] = None
                row["z_score"] = None
                continue
            mean_val = statistics.mean(prior_vals)
            sd_val = statistics.stdev(prior_vals) if len(prior_vals) >= 2 else 0.0
            row["baseline_mean"] = round(mean_val, 4)
            row["baseline_sd"] = round(sd_val, 4)
            if sd_val > 1e-6:
                row["z_score"] = round((row["value"] - mean_val) / sd_val, 4)
            else:
                row["z_score"] = 0.0


def compute_and_store_daily_metrics(
    conn: Any,
    user_id: int,
    days_back: int = 365,
) -> Dict[str, Any]:
    """Aggregate raw biomarkers & workouts into daily_metrics and update user profile rollups."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timezone, sleep_target_hours
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        )
        u_row = cur.fetchone()
        if not u_row:
            raise ValueError(f"User {user_id} not found")
        user_tz_str, sleep_target = u_row[0], float(u_row[1] or 7.5)

    tz = resolve_user_tz(user_tz_str)
    today_local = datetime.now(tz).date()
    store_start_day = today_local - timedelta(days=days_back)
    fetch_start_day = store_start_day - timedelta(days=BASELINE_LOOKBACK_DAYS + 5)
    fetch_start_dt = datetime.combine(fetch_start_day, datetime.min.time(), tzinfo=tz)

    # 1. Load raw biomarkers over window
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_metric, source, device, recorded_at, value, value_min, value_max, unit, metadata
            FROM biomarkers
            WHERE user_id = %s
              AND canonical_metric IS NOT NULL
              AND value IS NOT NULL
              AND recorded_at >= %s
            ORDER BY recorded_at ASC
            """,
            (user_id, fetch_start_dt),
        )
        bio_rows = cur.fetchall()

        cur.execute(
            """
            SELECT started_at, duration_sec, calories, source, device
            FROM workouts
            WHERE user_id = %s
              AND started_at >= %s
            ORDER BY started_at ASC
            """,
            (user_id, fetch_start_dt),
        )
        workout_rows = cur.fetchall()

    sleep_buckets: Dict[Tuple[date, str, str], List[Tuple[datetime, float, Dict[str, Any]]]] = (
        defaultdict(list)
    )
    additive_buckets: Dict[Tuple[date, str, str, str], Dict[str, Any]] = {}
    level_buckets: Dict[Tuple[date, str, str, str], Dict[str, Any]] = {}
    monitored_days: Set[date] = set()

    for canonical, source, device, recorded_at, val, val_min, val_max, unit, meta in bio_rows:
        f_val = float(val)
        local_dt = recorded_at.astimezone(tz)
        dev_str = device or ""
        src_str = source or ""

        if canonical in {"skin_temperature_delta", "skin_temperature"}:
            canonical, f_val, val_min, val_max, unit = _normalize_temperature_point(
                canonical, f_val, val_min, val_max, unit
            )

        if canonical == "sleep_duration":
            sleep_night = (local_dt + timedelta(hours=6)).date()
            sleep_buckets[(sleep_night, src_str, dev_str)].append(
                (recorded_at, f_val, meta if isinstance(meta, dict) else {})
            )
            continue

        day_local = local_dt.date()
        if canonical in {"steps", "active_calories", "heart_rate", "exercise_minutes"}:
            monitored_days.add(day_local)

        if canonical in ADDITIVE_METRICS:
            bkey = (day_local, canonical, src_str, dev_str)
            bucket = additive_buckets.get(bkey)
            if bucket is None:
                additive_buckets[bkey] = {
                    "sum": f_val,
                    "count": 1,
                    "unit": unit,
                }
            else:
                bucket["sum"] += f_val
                bucket["count"] += 1
        elif canonical in LEVEL_METRICS:
            bkey = (day_local, canonical, src_str, dev_str)
            lo = float(val_min) if val_min is not None else f_val
            hi = float(val_max) if val_max is not None else f_val
            bucket = level_buckets.get(bkey)
            if bucket is None:
                level_buckets[bkey] = {
                    "vals": [f_val],
                    "min": lo,
                    "max": hi,
                    "unit": unit,
                }
            else:
                bucket["vals"].append(f_val)
                if lo < bucket["min"]:
                    bucket["min"] = lo
                if hi > bucket["max"]:
                    bucket["max"] = hi

    computed_rows: List[Dict[str, Any]] = []
    nightly_sleep_by_day: Dict[date, List[Tuple[str, str, float]]] = defaultdict(list)

    # Sleep duration per (night, source, device)
    for (sleep_night, src_str, dev_str), segs in sleep_buckets.items():
        hours, sleep_meta = compute_sleep_night_hours(segs)
        if hours <= 0.0:
            continue
        nightly_sleep_by_day[sleep_night].append((src_str, dev_str, hours))
        computed_rows.append(
            {
                "day": sleep_night,
                "canonical_metric": "sleep_duration",
                "source": src_str,
                "device": dev_str,
                "value": hours,
                "value_min": None,
                "value_max": None,
                "sample_count": sleep_meta["raw_segments"],
                "unit": "hr",
                "metadata": sleep_meta,
            }
        )

    # 14-day cumulative sleep debt strictly over observed nights
    observed_sleep_days: Dict[date, float] = {}
    for sday, dev_list in nightly_sleep_by_day.items():
        sel = _select_night_sleep_hours(dev_list)
        if sel is not None:
            observed_sleep_days[sday] = sel

    for sday in sorted(observed_sleep_days):
        win_start = sday - timedelta(days=SLEEP_DEBT_WINDOW_DAYS - 1)
        obs_in_win = [
            observed_sleep_days[d]
            for d in range_days(win_start, sday)
            if d in observed_sleep_days
        ]
        if not obs_in_win:
            continue
        debt = 0.0
        for h in obs_in_win:
            if h < sleep_target:
                debt += sleep_target - h
            else:
                debt = max(0.0, debt - 0.5 * (h - sleep_target))
        computed_rows.append(
            {
                "day": sday,
                "canonical_metric": "sleep_debt",
                "source": "prana",
                "device": "",
                "value": round(max(0.0, debt), 3),
                "value_min": None,
                "value_max": None,
                "sample_count": len(obs_in_win),
                "unit": "hr",
                "metadata": {
                    "target_hours": sleep_target,
                    "observed_nights": len(obs_in_win),
                    "window_days": SLEEP_DEBT_WINDOW_DAYS,
                    "night_sleep_hours": observed_sleep_days[sday],
                },
            }
        )

    # Additive metrics
    for (day_local, canonical, src_str, dev_str), bucket in additive_buckets.items():
        computed_rows.append(
            {
                "day": day_local,
                "canonical_metric": canonical,
                "source": src_str,
                "device": dev_str,
                "value": round(bucket["sum"], 4),
                "value_min": None,
                "value_max": None,
                "sample_count": bucket["count"],
                "unit": bucket["unit"],
                "metadata": {},
            }
        )

    # Level metrics + ln(HRV)
    for (day_local, canonical, src_str, dev_str), bucket in level_buckets.items():
        mean_val = round(statistics.mean(bucket["vals"]), 4)
        computed_rows.append(
            {
                "day": day_local,
                "canonical_metric": canonical,
                "source": src_str,
                "device": dev_str,
                "value": mean_val,
                "value_min": round(bucket["min"], 4),
                "value_max": round(bucket["max"], 4),
                "sample_count": len(bucket["vals"]),
                "unit": bucket["unit"],
                "metadata": {},
            }
        )
        if canonical in {"hrv_sdnn", "hrv_rmssd"} and mean_val > 0.0:
            computed_rows.append(
                {
                    "day": day_local,
                    "canonical_metric": "hrv_ln",
                    "source": src_str,
                    "device": dev_str,
                    "value": round(math.log(mean_val), 4),
                    "value_min": None,
                    "value_max": None,
                    "sample_count": len(bucket["vals"]),
                    "unit": "ln(ms)",
                    "metadata": {"derived_from": canonical},
                }
            )

    # Workouts -> daily workout_load & ACWR strictly on monitored days
    workout_loads_by_day: Dict[date, Dict[str, Any]] = {}
    for started_at, dur_sec, cals, _src, _dev in workout_rows:
        wday = started_at.astimezone(tz).date()
        monitored_days.add(wday)
        load = _estimate_workout_load(dur_sec, cals)
        if load <= 0.0:
            continue
        entry = workout_loads_by_day.get(wday)
        if entry is None:
            workout_loads_by_day[wday] = {"load": load, "count": 1}
        else:
            entry["load"] += load
            entry["count"] += 1

    for wday, entry in workout_loads_by_day.items():
        computed_rows.append(
            {
                "day": wday,
                "canonical_metric": "workout_load",
                "source": "prana",
                "device": "",
                "value": round(entry["load"], 2),
                "value_min": None,
                "value_max": None,
                "sample_count": entry["count"],
                "unit": "load",
                "metadata": {},
            }
        )

    for mday in sorted(monitored_days):
        acute_days = [
            d
            for d in range_days(mday - timedelta(days=WORKOUT_ACUTE_DAYS - 1), mday)
            if d in monitored_days
        ]
        chronic_days = [
            d
            for d in range_days(mday - timedelta(days=WORKOUT_CHRONIC_DAYS - 1), mday)
            if d in monitored_days
        ]
        if len(acute_days) < 3 or len(chronic_days) < 14:
            continue
        chronic_loads = [workout_loads_by_day[d]["load"] if d in workout_loads_by_day else 0.0 for d in chronic_days]
        acute_loads = [workout_loads_by_day[d]["load"] if d in workout_loads_by_day else 0.0 for d in acute_days]
        chronic_mean = statistics.mean(chronic_loads)
        if chronic_mean < 5.0:
            continue
        acute_mean = statistics.mean(acute_loads)
        acwr_val = round(acute_mean / chronic_mean, 3)
        computed_rows.append(
            {
                "day": mday,
                "canonical_metric": "acwr",
                "source": "prana",
                "device": "",
                "value": acwr_val,
                "value_min": None,
                "value_max": None,
                "sample_count": len(chronic_days),
                "unit": "ratio",
                "metadata": {
                    "acute_daily_load": round(acute_mean, 2),
                    "chronic_daily_load": round(chronic_mean, 2),
                    "monitored_days_28d": len(chronic_days),
                },
            }
        )

    # Apply rolling baselines across the full fetched window before filtering to store_start_day
    _apply_rolling_baselines(computed_rows)

    rows_to_upsert = [r for r in computed_rows if r["day"] >= store_start_day]
    with conn.cursor() as cur:
        # Remove previously derived synthetic rows within the recomputed store window so stale days vanish
        cur.execute(
            """
            DELETE FROM daily_metrics
            WHERE user_id = %s
              AND day >= %s
            """,
            (user_id, store_start_day),
        )
        if rows_to_upsert:
            params = [
                (
                    user_id,
                    r["day"],
                    r["canonical_metric"],
                    r["source"],
                    r["device"],
                    r["value"],
                    r["value_min"],
                    r["value_max"],
                    r["sample_count"],
                    r["unit"],
                    r.get("baseline_mean"),
                    r.get("baseline_sd"),
                    r.get("z_score"),
                    json.dumps(r["metadata"]),
                )
                for r in rows_to_upsert
            ]
            cur.executemany(
                """
                INSERT INTO daily_metrics (
                    user_id, day, canonical_metric, source, device,
                    value, value_min, value_max, sample_count, unit,
                    baseline_mean, baseline_sd, z_score, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (user_id, day, canonical_metric, source, device)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    value_min = EXCLUDED.value_min,
                    value_max = EXCLUDED.value_max,
                    sample_count = EXCLUDED.sample_count,
                    unit = EXCLUDED.unit,
                    baseline_mean = EXCLUDED.baseline_mean,
                    baseline_sd = EXCLUDED.baseline_sd,
                    z_score = EXCLUDED.z_score,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                params,
            )

        # Update user's latest_vo2_max and sleep_debt_hours strictly from latest observed records
        cur.execute(
            """
            SELECT value FROM daily_metrics
            WHERE user_id = %s AND canonical_metric = 'vo2_max'
            ORDER BY day DESC, updated_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        vo2_row = cur.fetchone()
        latest_vo2 = float(vo2_row[0]) if vo2_row else None

        cur.execute(
            """
            SELECT value FROM daily_metrics
            WHERE user_id = %s AND canonical_metric = 'sleep_debt'
            ORDER BY day DESC, updated_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        debt_row = cur.fetchone()
        latest_debt = float(debt_row[0]) if debt_row else None

        cur.execute(
            """
            UPDATE users
            SET latest_vo2_max = %s,
                sleep_debt_hours = %s
            WHERE id = %s
            """,
            (latest_vo2, latest_debt, user_id),
        )

    days_touched = len({r["day"] for r in rows_to_upsert})
    logger.info(
        "Rolled up %d daily_metrics rows across %d days for user_id=%d",
        len(rows_to_upsert),
        days_touched,
        user_id,
    )
    return {
        "rows_upserted": len(rows_to_upsert),
        "days_processed": days_touched,
        "latest_vo2_max": latest_vo2,
        "sleep_debt_hours": latest_debt,
    }


def range_days(start: date, end: date) -> Iterable[date]:
    """Yield calendar dates inclusively from start through end."""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)
