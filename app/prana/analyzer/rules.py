from collections import defaultdict
from datetime import date, datetime, timedelta
import json
import math
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from prana.analyzer.rollup import resolve_user_tz
from prana.logging_setup import get_logger

logger = get_logger(__name__)

# ACSM-style CRF cutoffs: (fair_min, good_min, excellent_min, superior_min)
_VO2_NORMS: Dict[str, List[Tuple[int, Tuple[float, float, float, float]]]] = {
    "male": [
        (29, (38.1, 43.9, 48.0, 52.4)),
        (39, (35.5, 41.0, 44.9, 49.2)),
        (49, (33.0, 38.1, 42.1, 46.4)),
        (59, (29.0, 34.0, 37.9, 42.1)),
        (120, (25.0, 29.5, 33.4, 37.7)),
    ],
    "female": [
        (29, (31.0, 35.5, 39.5, 44.0)),
        (39, (28.0, 32.3, 35.8, 39.9)),
        (49, (25.0, 29.1, 32.8, 36.7)),
        (59, (22.0, 25.8, 29.2, 33.0)),
        (120, (20.0, 23.3, 26.5, 30.2)),
    ],
}


def _classify_vo2max(vo2: float, age: Optional[int], sex: Optional[str]) -> Tuple[str, str]:
    """Return (tier_label, bracket_label) using age/sex cardiorespiratory norms."""
    norm_sex = (sex or "").strip().lower()
    if norm_sex not in _VO2_NORMS:
        norm_sex = "male"
    eff_age = age if age is not None else 35
    cutoffs = _VO2_NORMS[norm_sex][-1][1]
    bracket = "60+"
    prev_age = 20
    for max_age, vals in _VO2_NORMS[norm_sex]:
        if eff_age <= max_age:
            cutoffs = vals
            bracket = f"{prev_age}-{max_age}" if max_age < 120 else "60+"
            break
        prev_age = max_age + 1

    fair_min, good_min, exc_min, sup_min = cutoffs
    if vo2 >= sup_min:
        tier = "Superior (>80th pct)"
    elif vo2 >= exc_min:
        tier = "Excellent (60-80th pct)"
    elif vo2 >= good_min:
        tier = "Good (40-60th pct)"
    elif vo2 >= fair_min:
        tier = "Fair (20-40th pct)"
    else:
        tier = "Poor (<20th pct)"
    return tier, f"{norm_sex.title()} {bracket}"


def _rankdata(xs: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(xs), key=lambda p: p[1])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def spearman_corr(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Compute tie-adjusted Spearman rank correlation coefficient."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    rx = _rankdata(xs)
    ry = _rankdata(ys)
    mx = statistics.mean(rx)
    my = statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den_x = sum((a - mx) ** 2 for a in rx)
    den_y = sum((b - my) ** 2 for b in ry)
    den = math.sqrt(den_x * den_y)
    if den <= 1e-12:
        return None
    return round(max(-1.0, min(1.0, num / den)), 3)


def _pick_latest_row(
    rows: Sequence[Dict[str, Any]],
    allowed_days: Sequence[date],
    prefer_z: bool = False,
) -> Optional[Dict[str, Any]]:
    """Return latest observation on the first day in allowed_days that has data."""
    for d in allowed_days:
        candidates = [r for r in rows if r["day"] == d]
        if not candidates:
            continue
        if prefer_z:
            with_z = [r for r in candidates if r.get("z_score") is not None]
            if with_z:
                candidates = with_z
        # Prefer largest sample_count then Apple Watch / Oura deterministic order
        candidates.sort(key=lambda r: (r["sample_count"], r["source"], r["device"]), reverse=True)
        return candidates[0]
    return None


def _is_apple_watch_sleep(row: Dict[str, Any]) -> bool:
    return row["source"] == "apple_health" and "oura" not in (row["device"] or "").lower()


def _is_oura_sleep(row: Dict[str, Any]) -> bool:
    return row["source"] == "oura_ble" or "oura" in (row["device"] or "").lower()


def evaluate_and_store_insights(
    conn: Any,
    user_id: int,
    target_day: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Run evidence-based physiological rules A-H and persist results in insights."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timezone, date_of_birth, sex, sleep_target_hours
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        )
        u_row = cur.fetchone()
        if not u_row:
            raise ValueError(f"User {user_id} not found")
        tz_str, dob, sex, sleep_target = u_row[0], u_row[1], u_row[2], float(u_row[3] or 7.5)

    tz = resolve_user_tz(tz_str)
    eval_day = target_day or datetime.now(tz).date()
    lookback_start = eval_day - timedelta(days=90)
    recent_days = [eval_day, eval_day - timedelta(days=1)]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT day, canonical_metric, source, device, value, value_min, value_max,
                   sample_count, unit, baseline_mean, baseline_sd, z_score
            FROM daily_metrics
            WHERE user_id = %s
              AND day >= %s
              AND day <= %s
            ORDER BY day ASC
            """,
            (user_id, lookback_start, eval_day),
        )
        raw_rows = cur.fetchall()

    by_metric: Dict[str, List[Dict[str, Any]]] = {}
    for (
        d,
        metric,
        src,
        dev,
        val,
        vmin,
        vmax,
        scount,
        unit,
        bmean,
        bsd,
        z,
    ) in raw_rows:
        by_metric.setdefault(metric, []).append(
            {
                "day": d,
                "canonical_metric": metric,
                "source": src or "",
                "device": dev or "",
                "value": float(val),
                "value_min": float(vmin) if vmin is not None else None,
                "value_max": float(vmax) if vmax is not None else None,
                "sample_count": int(scount),
                "unit": unit,
                "baseline_mean": float(bmean) if bmean is not None else None,
                "baseline_sd": float(bsd) if bsd is not None else None,
                "z_score": float(z) if z is not None else None,
            }
        )

    insights: List[Dict[str, Any]] = []

    # Rule A: Autonomic Readiness (ln HRV + Resting Heart Rate, Plews et al. 2013)
    hrv_row = _pick_latest_row(by_metric.get("hrv_ln", []), recent_days, prefer_z=True)
    hrv_raw_rows = by_metric.get("hrv_sdnn", []) + by_metric.get("hrv_rmssd", [])
    hrv_ms_row = (
        next(
            (
                r
                for r in hrv_raw_rows
                if hrv_row
                and r["day"] == hrv_row["day"]
                and r["source"] == hrv_row["source"]
                and r["device"] == hrv_row["device"]
            ),
            None,
        )
        if hrv_row
        else None
    )
    rhr_row = _pick_latest_row(by_metric.get("resting_heart_rate", []), recent_days, prefer_z=True)
    if hrv_row and rhr_row and hrv_row["z_score"] is not None and rhr_row["z_score"] is not None:
        hz = hrv_row["z_score"]
        rz = rhr_row["z_score"]
        ms_txt = f"{hrv_ms_row['value']:.0f} ms" if hrv_ms_row else f"ln={hrv_row['value']:.2f}"
        if hz <= -1.0 and rz >= 1.0:
            insights.append(
                {
                    "rule_id": "recovery_autonomic",
                    "severity": "alert",
                    "title": "Suppressed HRV & Elevated RHR (Sympathetic Strain)",
                    "finding": (
                        f"HRV ({ms_txt}, z={hz:+.2f}) dropped below baseline while resting HR "
                        f"({rhr_row['value']:.0f} bpm, z={rz:+.2f}) rose significantly."
                    ),
                    "action": "Switch to complete rest or gentle Zone 1 mobility today and aim for bed 45-60 min early.",
                    "evidence": {"hrv_z": hz, "rhr_z": rz, "rhr": rhr_row["value"], "day": str(hrv_row["day"])},
                }
            )
        elif hz <= -0.5 or rz >= 0.75:
            insights.append(
                {
                    "rule_id": "recovery_autonomic",
                    "severity": "watch",
                    "title": "Mild Autonomic Fatigue",
                    "finding": (
                        f"Autonomic balance shows mild strain (HRV {ms_txt}, z={hz:+.2f}; "
                        f"RHR {rhr_row['value']:.0f} bpm, z={rz:+.2f})."
                    ),
                    "action": "Keep training capped at Zone 2 aerobic steady-state and avoid maximal intervals today.",
                    "evidence": {"hrv_z": hz, "rhr_z": rz, "rhr": rhr_row["value"], "day": str(hrv_row["day"])},
                }
            )
        elif hz >= 0.5 and rz <= 0.0:
            insights.append(
                {
                    "rule_id": "recovery_autonomic",
                    "severity": "info",
                    "title": "High Autonomic Readiness",
                    "finding": (
                        f"Strong parasympathetic tone (HRV {ms_txt}, z={hz:+.2f}; "
                        f"RHR {rhr_row['value']:.0f} bpm, z={rz:+.2f})."
                    ),
                    "action": "Green light for quality intensity, threshold work, or progressive strength training.",
                    "evidence": {"hrv_z": hz, "rhr_z": rz, "rhr": rhr_row["value"], "day": str(hrv_row["day"])},
                }
            )

    # Rule B: Multi-Signal Early Illness / Systemic Strain Warning
    strain_signals: List[str] = []
    strain_evidence: Dict[str, Any] = {}
    if rhr_row and rhr_row["z_score"] is not None and rhr_row["z_score"] >= 1.0:
        strain_signals.append(f"RHR +{rhr_row['z_score']:.1f} SD ({rhr_row['value']:.0f} bpm)")
        strain_evidence["rhr_z"] = rhr_row["z_score"]
    resp_row = _pick_latest_row(by_metric.get("respiratory_rate", []), recent_days, prefer_z=True)
    if resp_row and (
        (resp_row["z_score"] is not None and resp_row["z_score"] >= 1.0)
        or (resp_row["baseline_mean"] is not None and resp_row["value"] - resp_row["baseline_mean"] >= 1.0)
    ):
        strain_signals.append(f"Resp rate {resp_row['value']:.1f} br/min")
        strain_evidence["respiratory_rate"] = resp_row["value"]
    temp_row = _pick_latest_row(by_metric.get("skin_temperature_delta", []), recent_days, prefer_z=True)
    abs_temp_row = _pick_latest_row(by_metric.get("skin_temperature", []), recent_days, prefer_z=True)
    if temp_row and (
        temp_row["value"] >= 0.5 or (temp_row["z_score"] is not None and temp_row["z_score"] >= 1.2)
    ):
        strain_signals.append(f"Wrist/skin temp {temp_row['value']:+.2f} °C")
        strain_evidence["skin_temperature_delta"] = temp_row["value"]
    elif abs_temp_row and abs_temp_row["baseline_mean"] is not None and (
        (abs_temp_row["z_score"] is not None and abs_temp_row["z_score"] >= 1.2)
        or (abs_temp_row["value"] - abs_temp_row["baseline_mean"] >= 0.5)
    ):
        temp_dev = round(abs_temp_row["value"] - abs_temp_row["baseline_mean"], 2)
        strain_signals.append(f"Wrist/skin temp {temp_dev:+.2f} °C vs baseline")
        strain_evidence["skin_temperature_dev"] = temp_dev
    spo2_row = _pick_latest_row(by_metric.get("spo2", []), recent_days, prefer_z=False)
    if spo2_row and spo2_row["value"] < 95.0:
        strain_signals.append(f"SpO2 {spo2_row['value']:.1f}%")
        strain_evidence["spo2"] = spo2_row["value"]
    if hrv_row and hrv_row["z_score"] is not None and hrv_row["z_score"] <= -1.0:
        strain_signals.append(f"HRV {hrv_row['z_score']:.1f} SD")
        strain_evidence["hrv_z"] = hrv_row["z_score"]

    if len(strain_signals) >= 2:
        insights.append(
            {
                "rule_id": "illness_early_warning",
                "severity": "alert",
                "title": "Multi-Marker Systemic Strain / Illness Alert",
                "finding": f"{len(strain_signals)} physiological strain signals flagged together: {', '.join(strain_signals)}.",
                "action": "Skip intense workouts today, hydrate with electrolytes, monitor temperature/symptoms, and prioritize sleep.",
                "evidence": {"signals": strain_signals, **strain_evidence},
            }
        )
    elif (
        (temp_row and temp_row["value"] >= 0.8)
        or (
            abs_temp_row
            and abs_temp_row["baseline_mean"] is not None
            and abs_temp_row["value"] - abs_temp_row["baseline_mean"] >= 0.8
        )
        or (spo2_row and spo2_row["value"] < 94.0)
        or (resp_row and resp_row["z_score"] is not None and resp_row["z_score"] >= 1.8)
    ):
        insights.append(
            {
                "rule_id": "illness_early_warning",
                "severity": "watch",
                "title": "Single Strong Systemic Strain Signal",
                "finding": f"Isolated pronounced marker deviation: {', '.join(strain_signals)}.",
                "action": "Keep exertion light today and re-check temperature, respiratory rate, and resting heart rate tomorrow morning.",
                "evidence": {"signals": strain_signals, **strain_evidence},
            }
        )

    # Rule C: VO2max Cardiorespiratory Fitness Tier (ACSM / Mandsager et al. JAMA 2018)
    vo2_rows = sorted(by_metric.get("vo2_max", []), key=lambda r: r["day"])
    if vo2_rows:
        latest_vo2 = vo2_rows[-1]
        age: Optional[int] = None
        if isinstance(dob, date):
            age = eval_day.year - dob.year - ((eval_day.month, eval_day.day) < (dob.month, dob.day))
        tier, bracket = _classify_vo2max(latest_vo2["value"], age, sex)
        older = [r for r in vo2_rows if r["day"] <= latest_vo2["day"] - timedelta(days=20)]
        delta_txt = ""
        delta_30d: Optional[float] = None
        if older:
            delta_30d = round(latest_vo2["value"] - older[-1]["value"], 2)
            delta_txt = f" ({delta_30d:+.1f} vs ~30d prior)"
        sev = "watch" if tier.startswith(("Poor", "Fair")) else "info"
        insights.append(
            {
                "rule_id": "cardiorespiratory_vo2max",
                "severity": sev,
                "title": f"VO2max Fitness: {tier}",
                "finding": (
                    f"Latest VO2max is {latest_vo2['value']:.1f} ml/kg/min{delta_txt} "
                    f"({tier} for {bracket})."
                ),
                "action": (
                    "Target 150-180 min/wk Zone 2 aerobic base plus one weekly 4x4 min high-intensity "
                    "interval session (on well-recovered days) to sustain or raise VO2max."
                ),
                "evidence": {
                    "vo2_max": latest_vo2["value"],
                    "day": str(latest_vo2["day"]),
                    "age": age,
                    "sex": sex,
                    "tier": tier,
                    "bracket": bracket,
                    "delta_30d": delta_30d,
                },
            }
        )

    # Rule D: 14-Day Cumulative Sleep Debt (Kitamura et al. 2016)
    debt_rows = sorted(by_metric.get("sleep_debt", []), key=lambda r: r["day"])
    if debt_rows and (eval_day - debt_rows[-1]["day"]).days <= 2:
        latest_debt = debt_rows[-1]
        debt_val = latest_debt["value"]
        if debt_val >= 5.0:
            sev = "alert"
            title = f"High Sleep Debt ({debt_val:.1f} hr)"
            action = (
                f"Move bedtime 45-60 min earlier tonight toward >{sleep_target:.1f} hr sleep, "
                "halt caffeine >=10h pre-bed, and get morning outdoor light."
            )
        elif debt_val >= 2.0:
            sev = "watch"
            title = f"Moderate Sleep Debt ({debt_val:.1f} hr)"
            action = (
                f"Protect an extra 30-45 min sleep opportunity tonight above your {sleep_target:.1f} hr target "
                "to pay down accumulated debt before it impairs HRV and insulin sensitivity."
            )
        else:
            sev = "info"
            title = f"Low Sleep Debt ({debt_val:.1f} hr)"
            action = f"Maintain your consistent sleep schedule anchored around {sleep_target:.1f} hr/night."
        insights.append(
            {
                "rule_id": "sleep_debt_tracker",
                "severity": sev,
                "title": title,
                "finding": (
                    f"14-day cumulative sleep debt stands at {debt_val:.1f} hr across observed nights "
                    f"(as of {latest_debt['day']}; unrecorded nights excluded)."
                ),
                "action": action,
                "evidence": {
                    "sleep_debt_hours": debt_val,
                    "target_hours": sleep_target,
                    "day": str(latest_debt["day"]),
                },
            }
        )

    # Rule E: Training Load ACWR (Gabbett 2016)
    acwr_row = _pick_latest_row(by_metric.get("acwr", []), recent_days, prefer_z=False)
    if acwr_row:
        acwr = acwr_row["value"]
        if acwr > 1.5:
            sev = "alert"
            title = f"Acute Training Load Spike (ACWR {acwr:.2f})"
            action = "Deload volume and intensity by 30-40% over the next 48-72h to reduce overuse injury risk."
        elif acwr > 1.3:
            sev = "watch"
            title = f"Elevated Training Ramp (ACWR {acwr:.2f})"
            action = "Hold weekly volume steady this week rather than stacking extra high-intensity sessions."
        elif acwr >= 0.8:
            sev = "info"
            title = f"Optimal Training Load Sweet Spot (ACWR {acwr:.2f})"
            action = "Continue current progressive training rhythm while honoring recovery signals."
        else:
            sev = "watch"
            title = f"Low Acute Workload / Taper (ACWR {acwr:.2f})"
            action = "If autonomic readiness is green, gradually layer 1-2 structured Zone 2 or strength sessions."
        insights.append(
            {
                "rule_id": "training_load_acwr",
                "severity": sev,
                "title": title,
                "finding": f"7d:28d Acute:Chronic Workload Ratio is {acwr:.2f} (sweet spot 0.80-1.30).",
                "action": action,
                "evidence": {"acwr": acwr, "day": str(acwr_row["day"])},
            }
        )

    # Rule F: Sedentary Guardrail & Daily Step Consistency (Paluch et al. Lancet 2022)
    step_cutoff = eval_day - timedelta(days=6)
    steps_by_day: Dict[date, float] = {}
    for r in by_metric.get("steps", []):
        if step_cutoff <= r["day"] <= eval_day:
            cur_max = steps_by_day.get(r["day"], 0.0)
            if r["value"] > cur_max:
                steps_by_day[r["day"]] = r["value"]
    if len(steps_by_day) >= 3:
        avg_steps = statistics.mean(steps_by_day.values())
        if avg_steps < 5000:
            sev = "watch"
            title = f"Low 7-Day Step Average ({avg_steps:,.0f}/day)"
            action = "Add a brisk 15-20 min post-meal walk today to break sedentary streaks and reach 7,500+ steps."
        elif avg_steps < 8000:
            sev = "info"
            title = f"Moderate Activity ({avg_steps:,.0f} steps/day)"
            action = "One extra 15-min walk brings your 7-day rolling step average into the 8,000+ longevity band."
        else:
            sev = "info"
            title = f"Strong Daily Movement ({avg_steps:,.0f} steps/day)"
            action = "Step volume over observed days sits in the optimal mortality-reduction range (8,000+/day)."
        insights.append(
            {
                "rule_id": "sedentary_activity",
                "severity": sev,
                "title": title,
                "finding": (
                    f"Averaged {avg_steps:,.0f} daily steps across {len(steps_by_day)} observed days "
                    "in the past week (missing days excluded)."
                ),
                "action": action,
                "evidence": {"avg_steps_7d": round(avg_steps, 0), "observed_days": len(steps_by_day)},
            }
        )

    # Rule G: Personal Lagged Biomarker Correlations (Spearman rank over observed pairs)
    # Collapse to single daily representative per day for sleep_duration, hrv_ln, resting_heart_rate, steps
    sleep_vals_by_day: Dict[date, List[float]] = defaultdict(list)
    for r in by_metric.get("sleep_duration", []):
        if r["value"] >= 2.0:
            sleep_vals_by_day[r["day"]].append(r["value"])
    daily_sleep_map: Dict[date, float] = {
        d: statistics.mean(vals) for d, vals in sleep_vals_by_day.items()
    }

    rhr_vals_by_day: Dict[date, List[float]] = defaultdict(list)
    for r in by_metric.get("resting_heart_rate", []):
        rhr_vals_by_day[r["day"]].append(r["value"])
    daily_rhr_map: Dict[date, float] = {
        d: statistics.mean(vals) for d, vals in rhr_vals_by_day.items()
    }

    hrv_vals_by_day: Dict[date, List[float]] = defaultdict(list)
    for r in by_metric.get("hrv_ln", []):
        hrv_vals_by_day[r["day"]].append(r["value"])
    daily_hrv_map: Dict[date, float] = {
        d: statistics.mean(vals) for d, vals in hrv_vals_by_day.items()
    }

    daily_steps_map: Dict[date, float] = {}
    for r in by_metric.get("steps", []):
        daily_steps_map[r["day"]] = max(daily_steps_map.get(r["day"], 0.0), r["value"])

    corr_candidates: List[Tuple[str, str, float, int]] = []
    # Pair 1: sleep night D -> RHR day D
    p1 = [(daily_sleep_map[d], daily_rhr_map[d]) for d in sorted(daily_sleep_map) if d in daily_rhr_map]
    if len(p1) >= 10:
        rho = spearman_corr([a for a, _ in p1], [b for _, b in p1])
        if rho is not None and abs(rho) >= 0.30:
            corr_candidates.append(("Sleep duration -> Same-day RHR", "sleep_vs_rhr", rho, len(p1)))

    # Pair 2: sleep night D -> HRV(ln) day D
    p2 = [(daily_sleep_map[d], daily_hrv_map[d]) for d in sorted(daily_sleep_map) if d in daily_hrv_map]
    if len(p2) >= 10:
        rho = spearman_corr([a for a, _ in p2], [b for _, b in p2])
        if rho is not None and abs(rho) >= 0.30:
            corr_candidates.append(("Sleep duration -> Morning HRV", "sleep_vs_hrv", rho, len(p2)))

    # Pair 3: steps day D-1 -> sleep night D
    p3 = [
        (daily_steps_map[d - timedelta(days=1)], daily_sleep_map[d])
        for d in sorted(daily_sleep_map)
        if (d - timedelta(days=1)) in daily_steps_map
    ]
    if len(p3) >= 10:
        rho = spearman_corr([a for a, _ in p3], [b for _, b in p3])
        if rho is not None and abs(rho) >= 0.30:
            corr_candidates.append(("Prior-day steps -> Next-night sleep", "steps_vs_next_sleep", rho, len(p3)))

    if corr_candidates:
        corr_candidates.sort(key=lambda item: abs(item[2]), reverse=True)
        top_label, top_key, top_rho, top_n = corr_candidates[0]
        direction = "positively" if top_rho > 0 else "inversely"
        summary_parts = [f"{lbl} (r={r:+.2f}, n={n})" for lbl, _k, r, n in corr_candidates[:3]]
        insights.append(
            {
                "rule_id": "personal_correlations",
                "severity": "info",
                "title": f"Personal Pattern: {top_label} (rho={top_rho:+.2f})",
                "finding": (
                    f"Over {top_n} paired observed days, {top_label.lower()} correlates {direction} "
                    f"(Spearman rho={top_rho:+.2f}). Tracked pairs: {'; '.join(summary_parts)}."
                ),
                "action": "Use your strongest personal lever—prioritize consistent sleep duration on training days to protect next-day recovery.",
                "evidence": {
                    "top_pair": top_key,
                    "top_rho": top_rho,
                    "n_pairs": top_n,
                    "all_significant": [
                        {"pair": k, "label": lbl, "rho": r, "n": n} for lbl, k, r, n in corr_candidates
                    ],
                },
            }
        )

    # Rule H: Apple Watch vs Oura Sleep Reconciliation Check
    watch_sleep: Dict[date, float] = {}
    oura_sleep: Dict[date, float] = {}
    for r in by_metric.get("sleep_duration", []):
        if _is_apple_watch_sleep(r):
            watch_sleep[r["day"]] = r["value"]
        elif _is_oura_sleep(r):
            oura_sleep[r["day"]] = r["value"]

    overlap_days = sorted(d for d in watch_sleep if d in oura_sleep and d >= eval_day - timedelta(days=14))
    if overlap_days:
        latest_ov = overlap_days[-1]
        if (eval_day - latest_ov).days <= 2:
            w_hr = watch_sleep[latest_ov]
            o_hr = oura_sleep[latest_ov]
            abs_diff = abs(w_hr - o_hr)
            rel_diff = abs_diff / max(w_hr, o_hr, 1e-6)
            mean_abs_14d = statistics.mean(abs(watch_sleep[d] - oura_sleep[d]) for d in overlap_days)

            if abs_diff >= 0.75 or rel_diff >= 0.15:
                insights.append(
                    {
                        "rule_id": "cross_device_sleep_gap",
                        "severity": "watch",
                        "title": f"Apple Watch vs Oura Sleep Gap ({abs_diff:.2f} hr)",
                        "finding": (
                            f"On {latest_ov}, Apple Watch logged {w_hr:.2f} hr vs Oura {o_hr:.2f} hr "
                            f"(gap {abs_diff:.2f} hr / {rel_diff * 100:.0f}%; 14d avg diff {mean_abs_14d:.2f} hr)."
                        ),
                        "action": (
                            "Check whether one wearable was removed/charging overnight, verify HealthKit sleep "
                            "permissions, and make sure both synced completely."
                        ),
                        "evidence": {
                            "day": str(latest_ov),
                            "apple_watch_hr": round(w_hr, 2),
                            "oura_hr": round(o_hr, 2),
                            "abs_diff_hr": round(abs_diff, 2),
                            "rel_diff_pct": round(rel_diff * 100, 1),
                            "avg_14d_diff_hr": round(mean_abs_14d, 2),
                        },
                    }
                )
            else:
                insights.append(
                    {
                        "rule_id": "cross_device_sleep_gap",
                        "severity": "info",
                        "title": f"Wearable Sleep Concordance Tight ({abs_diff:.2f} hr diff)",
                        "finding": (
                            f"On {latest_ov}, Apple Watch ({w_hr:.2f} hr) and Oura ({o_hr:.2f} hr) agreed closely "
                            f"(diff {abs_diff:.2f} hr; 14d avg diff {mean_abs_14d:.2f} hr)."
                        ),
                        "action": "Both wearables are tracking overnight sleep consistently.",
                        "evidence": {
                            "day": str(latest_ov),
                            "apple_watch_hr": round(w_hr, 2),
                            "oura_hr": round(o_hr, 2),
                            "abs_diff_hr": round(abs_diff, 2),
                            "avg_14d_diff_hr": round(mean_abs_14d, 2),
                        },
                    }
                )

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM insights WHERE user_id = %s AND day = %s",
            (user_id, eval_day),
        )
        if insights:
            cur.executemany(
                """
                INSERT INTO insights (user_id, day, rule_id, severity, title, finding, action, evidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                [
                    (
                        user_id,
                        eval_day,
                        ins["rule_id"],
                        ins["severity"],
                        ins["title"],
                        ins["finding"],
                        ins["action"],
                        json.dumps(ins["evidence"]),
                    )
                    for ins in insights
                ],
            )

    logger.info("Evaluated %d insights on %s for user_id=%d", len(insights), eval_day, user_id)
    return [{"day": str(eval_day), **ins} for ins in insights]
