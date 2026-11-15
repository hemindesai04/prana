import json
import statistics
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import psycopg

from prana.analyzer.rollup import compute_and_store_daily_metrics, resolve_user_tz
from prana.analyzer.rules import evaluate_and_store_insights
from prana.config import Settings
from prana.logging_setup import get_logger

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You are Prana, a quantitative physiology & longevity briefing analyst.
Analyze the user's JSON biomarker snapshot and evidence-based rule outputs using these strict clinical principles:
1. NEVER invent, extrapolate, or assume any number not present in the provided JSON.
2. Unrecorded metrics or missing nights mean "not recorded" — NEVER interpret them as 0 or normal.
3. Autonomic Readiness (Plews et al.): interpret ln(HRV) and Resting HR via their z-scores vs personal 60-day baseline (HRV z <= -1.0 with RHR z >= +1.0 = sympathetic strain; HRV z >= +0.5 and RHR z <= 0 = high readiness).
4. Sleep & Sleep Debt (Kitamura et al.): compare Apple Watch and Oura sleep separately when present; flag >=0.75h or >=15% gaps; report 14-day cumulative sleep debt and give concrete tonight/morning recovery actions.
5. Illness / Systemic Strain: watch >=2 co-elevated signals among RHR, respiratory rate, wrist/skin temp delta, low SpO2, or suppressed HRV.
6. Training Load (Gabbett ACWR) & Cardiorespiratory Fitness (ACSM VO2max age/sex tier): sweet spot ACWR is 0.80-1.30; >1.50 warrants a 48-72h deload.

Tone: concise, objective, practical, supportive.
Morning briefing goal: assess last night's sleep quality/devices, overnight recovery markers, sleep debt, and recommend exact training/recovery intensity for the day.
Evening briefing goal: summarize today's movement, workouts, daytime heart rate/stress vs baseline, current sleep debt, and set a specific wind-down/sleep goal for tonight.

Return ONLY valid JSON matching:
{
  "title": "Short headline (max 90 chars)",
  "summary_markdown": "Concise markdown summary (3-5 short bullets or 2 tight paragraphs)",
  "action_items": ["Concrete action 1", "Concrete action 2", "Concrete action 3"]
}"""


def _build_snapshot(
    conn: Any,
    user_id: int,
    period: str,
    eval_day: date,
) -> Dict[str, Any]:
    """Assemble strictly observed biomarker, workout, and insight snapshot for briefings."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT slug, display_name, timezone, date_of_birth, sex,
                   sleep_target_hours, latest_vo2_max, sleep_debt_hours
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        )
        u_row = cur.fetchone()
        if not u_row:
            raise ValueError(f"User {user_id} not found")
        slug, display_name, tz_str, dob, sex, sleep_target, latest_vo2, sleep_debt = u_row

        age: Optional[int] = None
        if isinstance(dob, date):
            age = eval_day.year - dob.year - ((eval_day.month, eval_day.day) < (dob.month, dob.day))

        start_7d = eval_day - timedelta(days=6)
        cur.execute(
            """
            SELECT day, canonical_metric, source, device, value, value_min, value_max,
                   baseline_mean, z_score, unit
            FROM daily_metrics
            WHERE user_id = %s
              AND day >= %s
              AND day <= %s
            ORDER BY day ASC
            """,
            (user_id, start_7d, eval_day),
        )
        metric_rows = cur.fetchall()

        tz = resolve_user_tz(tz_str)
        day_start = datetime.combine(eval_day, datetime.min.time(), tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        cur.execute(
            """
            SELECT workout_type, duration_sec, calories, source, started_at
            FROM workouts
            WHERE user_id = %s
              AND started_at >= %s
              AND started_at < %s
            ORDER BY started_at ASC
            """,
            (user_id, day_start, day_end),
        )
        workouts_today = [
            {
                "workout_type": r[0],
                "duration_min": round(float(r[1]) / 60.0, 1) if r[1] is not None else None,
                "calories": round(float(r[2]), 0) if r[2] is not None else None,
                "source": r[3],
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT rule_id, severity, title, finding, action, day
            FROM insights
            WHERE user_id = %s
              AND day >= %s
              AND day <= %s
            ORDER BY day DESC,
                     CASE severity WHEN 'alert' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END,
                     rule_id ASC
            """,
            (user_id, eval_day - timedelta(days=1), eval_day),
        )
        seen_rules = set()
        insights_list: List[Dict[str, Any]] = []
        for rule_id, sev, title, finding, action, iday in cur.fetchall():
            if rule_id in seen_rules:
                continue
            seen_rules.add(rule_id)
            insights_list.append(
                {
                    "rule_id": rule_id,
                    "day": str(iday),
                    "severity": sev,
                    "title": title,
                    "finding": finding,
                    "action": action,
                }
            )

    # Organize sleep last night & 7d observed average separately by device
    apple_sleep_7d: List[float] = []
    oura_sleep_7d: List[float] = []
    last_night_sleep: Dict[str, Any] = {}
    today_metrics: Dict[str, Any] = {}
    latest_vitals: Dict[str, Any] = {}

    for d, metric, src, dev, val, vmin, vmax, bmean, z, unit in metric_rows:
        fval = round(float(val), 3)
        entry = {
            "day": str(d),
            "value": fval,
            "unit": unit,
            "z_score": round(float(z), 2) if z is not None else None,
            "baseline_mean": round(float(bmean), 2) if bmean is not None else None,
            "source": src,
            "device": dev,
        }
        if vmin is not None and vmax is not None:
            entry["min"] = round(float(vmin), 2)
            entry["max"] = round(float(vmax), 2)

        if metric == "sleep_duration":
            is_oura = src == "oura_ble" or "oura" in (dev or "").lower()
            is_watch = src == "apple_health" and not is_oura
            if is_watch:
                apple_sleep_7d.append(fval)
                if d == eval_day:
                    last_night_sleep["apple_watch_hr"] = round(fval, 2)
            elif is_oura:
                oura_sleep_7d.append(fval)
                if d == eval_day:
                    last_night_sleep["oura_hr"] = round(fval, 2)
            continue

        if d == eval_day and metric in {
            "steps",
            "active_calories",
            "basal_calories",
            "exercise_minutes",
            "stand_minutes",
            "heart_rate",
            "workout_load",
            "acwr",
        }:
            prev = today_metrics.get(metric)
            # For additive metrics across multiple Apple devices, pick highest observed reading to avoid double-counting
            if prev is None or fval > prev["value"]:
                today_metrics[metric] = entry

        if metric in {
            "resting_heart_rate",
            "hrv_sdnn",
            "hrv_rmssd",
            "respiratory_rate",
            "spo2",
            "skin_temperature_delta",
            "skin_temperature",
            "vo2_max",
            "acwr",
        }:
            prev_v = latest_vitals.get(metric)
            if prev_v is None or str(d) > prev_v["day"] or (str(d) == prev_v["day"] and entry.get("z_score") is not None):
                latest_vitals[metric] = entry

    return {
        "user": {
            "slug": slug,
            "display_name": display_name,
            "age": age,
            "sex": sex,
            "sleep_target_hours": float(sleep_target or 7.5),
            "sleep_debt_hours": round(float(sleep_debt), 2) if sleep_debt is not None else None,
            "latest_vo2_max": round(float(latest_vo2), 1) if latest_vo2 is not None else None,
        },
        "period": period,
        "day": str(eval_day),
        "sleep": {
            "last_night": last_night_sleep or None,
            "apple_watch_7d_avg_hr": round(statistics.mean(apple_sleep_7d), 2) if apple_sleep_7d else None,
            "apple_watch_7d_nights": len(apple_sleep_7d),
            "oura_7d_avg_hr": round(statistics.mean(oura_sleep_7d), 2) if oura_sleep_7d else None,
            "oura_7d_nights": len(oura_sleep_7d),
        },
        "latest_vitals": latest_vitals,
        "today_activity": today_metrics or None,
        "workouts_today": workouts_today,
        "insights": insights_list,
    }


def _call_gemini_api(
    snapshot: Dict[str, Any],
    settings: Settings,
) -> Optional[Dict[str, Any]]:
    """Call Gemini REST API with structured JSON output; return None on missing key or error."""
    api_key = settings.gemini_api_key.strip()
    if not api_key:
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    user_prompt = (
        f"Generate the {snapshot['period']} briefing for date {snapshot['day']} from this verified data:\n"
        f"{json.dumps(snapshot, indent=2)}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 900,
            "responseMimeType": "application/json",
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
        parsed = json.loads(raw)
        candidates = parsed.get("candidates") or []
        if not candidates:
            logger.warning("Gemini returned no candidates")
            return None
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text_out = "".join(str(p.get("text", "")) for p in parts).strip()
        # Strip accidental markdown code fences if returned despite responseMimeType
        if text_out.startswith("```"):
            text_out = text_out.strip("`")
            if text_out.lower().startswith("json"):
                text_out = text_out[4:].strip()
        result = json.loads(text_out)
        if not isinstance(result, dict):
            return None
        title = str(result.get("title") or "").strip()
        summary = str(result.get("summary_markdown") or "").strip()
        actions = result.get("action_items") or []
        if not title or not summary:
            return None
        return {
            "title": title[:160],
            "summary_markdown": summary,
            "action_items": [str(a).strip() for a in actions if str(a).strip()][:5],
            "model_used": settings.gemini_model,
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.warning("Gemini API briefing failed, using deterministic fallback: %s", exc)
        return None


def _build_deterministic_briefing(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Build deterministic evidence-backed briefing when Gemini API key is unset or unavailable."""
    period = snapshot["period"]
    day_str = snapshot["day"]
    user = snapshot["user"]
    sleep = snapshot["sleep"]
    vitals = snapshot["latest_vitals"]
    today_act = snapshot["today_activity"] or {}
    workouts = snapshot["workouts_today"]
    insights = snapshot["insights"]

    debt = user.get("sleep_debt_hours")
    vo2 = user.get("latest_vo2_max")
    target_hr = user.get("sleep_target_hours", 7.5)

    lines: List[str] = []
    actions: List[str] = []

    if period == "morning":
        title = f"Morning Check-In ({day_str})"
        last_night = sleep.get("last_night")
        if last_night:
            parts = []
            if "apple_watch_hr" in last_night:
                parts.append(f"Apple Watch **{last_night['apple_watch_hr']:.2f} hr**")
            if "oura_hr" in last_night:
                parts.append(f"Oura **{last_night['oura_hr']:.2f} hr**")
            lines.append(f"- **Last Night's Sleep:** {', '.join(parts)} (target {target_hr:.1f} hr).")
        else:
            lines.append("- **Last Night's Sleep:** Not recorded yet (ignored in baselines & sleep debt).")

        if debt is not None:
            lines.append(f"- **14-Day Sleep Debt:** **{debt:.1f} hr**.")
        else:
            lines.append("- **14-Day Sleep Debt:** Not enough recorded nights yet.")

        vital_bits = []
        if "hrv_sdnn" in vitals:
            h = vitals["hrv_sdnn"]
            ztxt = f" (z={h['z_score']:+.2f})" if h.get("z_score") is not None else ""
            vital_bits.append(f"HRV SDNN {h['value']:.0f} ms{ztxt}")
        if "resting_heart_rate" in vitals:
            r = vitals["resting_heart_rate"]
            ztxt = f" (z={r['z_score']:+.2f})" if r.get("z_score") is not None else ""
            vital_bits.append(f"RHR {r['value']:.0f} bpm{ztxt}")
        if "skin_temperature_delta" in vitals:
            t = vitals["skin_temperature_delta"]
            vital_bits.append(f"Temp delta {t['value']:+.2f} °C")
        elif "skin_temperature" in vitals:
            t = vitals["skin_temperature"]
            if t.get("baseline_mean") is not None:
                dev = t["value"] - t["baseline_mean"]
                vital_bits.append(f"Wrist/skin temp {t['value']:.2f} °C ({dev:+.2f} vs base)")
            else:
                vital_bits.append(f"Wrist/skin temp {t['value']:.2f} °C")
        if vital_bits:
            lines.append(f"- **Readiness Vitals:** {'; '.join(vital_bits)}.")
        if vo2 is not None:
            lines.append(f"- **VO2max:** {vo2:.1f} ml/kg/min.")
    else:
        title = f"Evening Wrap-Up ({day_str})"
        act_bits = []
        if "steps" in today_act:
            act_bits.append(f"**{today_act['steps']['value']:,.0f}** steps")
        if "active_calories" in today_act:
            act_bits.append(f"**{today_act['active_calories']['value']:,.0f}** active kcal")
        if "exercise_minutes" in today_act:
            act_bits.append(f"**{today_act['exercise_minutes']['value']:.0f}** exercise min")
        if act_bits:
            lines.append(f"- **Today's Movement:** {', '.join(act_bits)}.")
        else:
            lines.append("- **Today's Movement:** No daytime activity synced yet for today.")

        if workouts:
            w_desc = ", ".join(
                f"{w['workout_type']} ({w['duration_min'] or '?'} min)" for w in workouts[:4]
            )
            lines.append(f"- **Workouts Logged:** {w_desc}.")

        if "heart_rate" in today_act:
            hr = today_act["heart_rate"]
            lines.append(
                f"- **Daytime Heart Rate:** avg {hr['value']:.0f} bpm "
                f"(range {hr.get('min', hr['value']):.0f}-{hr.get('max', hr['value']):.0f} bpm)."
            )
        if debt is not None:
            rec_sleep = round(min(9.5, target_hr + (0.5 if debt >= 2.0 else 0.0)), 1)
            lines.append(
                f"- **Sleep Debt Entering Tonight:** **{debt:.1f} hr** — aim for ~**{rec_sleep:.1f} hr** tonight."
            )

    alerts = [i for i in insights if i["severity"] in {"alert", "watch"}]
    for ins in alerts[:3]:
        lines.append(f"- **[{ins['severity'].upper()}] {ins['title']}:** {ins['finding']}")
        actions.append(ins["action"])

    for ins in insights:
        if ins["action"] not in actions and len(actions) < 3:
            actions.append(ins["action"])

    if not actions:
        actions.append(f"Aim for {target_hr:.1f} hr sleep tonight and sync your Apple Watch and Oura Ring.")

    return {
        "title": title,
        "summary_markdown": "\n".join(lines),
        "action_items": actions[:4],
        "model_used": "deterministic",
    }


def generate_briefing(
    conn: Any,
    user_id: int,
    period: str,
    settings: Settings,
    day: Optional[date] = None,
    skip_llm_if_no_sleep: bool = False,
) -> Dict[str, Any]:
    """Generate and store a morning or evening briefing for a single user."""
    norm_period = period.strip().lower()
    if norm_period not in {"morning", "evening"}:
        raise ValueError(f"Invalid briefing period: {period!r}")

    with conn.cursor() as cur:
        cur.execute("SELECT timezone FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"User {user_id} not found")
        tz = resolve_user_tz(row[0])

    eval_day = day or datetime.now(tz).date()
    snapshot = _build_snapshot(conn, user_id, norm_period, eval_day)
    # Release DB locks from rollup/queries before initiating external HTTP I/O.
    conn.commit()

    has_last_night_sleep = bool((snapshot.get("sleep") or {}).get("last_night"))
    if skip_llm_if_no_sleep and norm_period == "morning" and not has_last_night_sleep:
        generated = _build_deterministic_briefing(snapshot)
    else:
        generated = _call_gemini_api(snapshot, settings) or _build_deterministic_briefing(snapshot)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO briefings (
                user_id, day, period, title, summary_markdown,
                action_items, metrics_snapshot, model_used
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (user_id, day, period)
            DO UPDATE SET
                title = EXCLUDED.title,
                summary_markdown = EXCLUDED.summary_markdown,
                action_items = EXCLUDED.action_items,
                metrics_snapshot = EXCLUDED.metrics_snapshot,
                model_used = EXCLUDED.model_used,
                created_at = NOW()
            RETURNING id, created_at
            """,
            (
                user_id,
                eval_day,
                norm_period,
                generated["title"],
                generated["summary_markdown"],
                json.dumps(generated["action_items"]),
                json.dumps(snapshot),
                generated["model_used"],
            ),
        )
        bid, created_at = cur.fetchone()

    logger.info(
        "Stored %s briefing id=%d for user_id=%d day=%s via %s",
        norm_period,
        bid,
        user_id,
        eval_day,
        generated["model_used"],
    )
    return {
        "id": int(bid),
        "day": str(eval_day),
        "period": norm_period,
        "title": generated["title"],
        "summary_markdown": generated["summary_markdown"],
        "action_items": generated["action_items"],
        "model_used": generated["model_used"],
        "created_at": created_at.isoformat(),
    }


def maybe_complete_morning_briefing_after_sleep_sync(
    conn: Any,
    user_id: int,
    user_tz: str,
    settings: Settings,
) -> Optional[Dict[str, Any]]:
    """Run the single morning AI briefing once sleep arrives if morning AI hasn't run with sleep."""
    if not settings.gemini_api_key.strip():
        return None

    tz = resolve_user_tz(user_tz)
    now_local = datetime.now(tz)
    if not (settings.morning_briefing_hour <= now_local.hour < settings.evening_briefing_hour):
        return None

    today = now_local.date()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM daily_metrics
            WHERE user_id = %s AND day = %s AND canonical_metric = 'sleep_duration'
            LIMIT 1
            """,
            (user_id, today),
        )
        if cur.fetchone() is None:
            return None

        cur.execute(
            """
            SELECT model_used,
                   jsonb_typeof(metrics_snapshot #> '{sleep,last_night}') = 'object' AS had_sleep
            FROM briefings
            WHERE user_id = %s AND day = %s AND period = 'morning'
            LIMIT 1
            """,
            (user_id, today),
        )
        b_row = cur.fetchone()
        if b_row is not None and b_row[0] != "deterministic" and bool(b_row[1]):
            return None

    briefing = generate_briefing(conn, user_id, "morning", settings, day=today)
    conn.commit()
    return briefing


def run_scheduled_briefings(database_url: str, period: str, settings: Settings) -> None:
    """Scheduled job entrypoint: roll up metrics, evaluate insights, and build period briefings."""
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users ORDER BY id ASC")
                user_ids = [int(r[0]) for r in cur.fetchall()]

            for uid in user_ids:
                try:
                    compute_and_store_daily_metrics(conn, uid, days_back=60)
                    evaluate_and_store_insights(conn, uid)
                    conn.commit()
                    generate_briefing(
                        conn,
                        uid,
                        period,
                        settings,
                        skip_llm_if_no_sleep=(period == "morning"),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    logger.exception(
                        "Failed scheduled %s analysis/briefing for user_id=%d", period, uid
                    )
    except Exception:
        logger.exception("Scheduled %s briefing job failed connecting to database", period)
