from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from prana.db import bootstrap_users, hash_token


def test_health_and_status_endpoints(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    status_resp = client.get("/api/v1/status")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] in ("ok", "degraded")
    assert body["components"]["db"]["status"] == "ok"
    assert body["components"]["migrations"]["status"] == "ok"
    assert "open_issues" not in body

    assert client.get("/api/v1/status?detail=1").status_code == 401
    detail_resp = client.get(
        "/api/v1/status?detail=1",
        headers={"X-Admin-Token": "admin-secret"},
    )
    assert detail_resp.status_code == 200
    assert "open_issues" in detail_resp.json()


def test_ingest_requires_valid_token(client):
    assert client.post("/api/v1/ingest/apple-health", json={"data": {}}).status_code == 401
    assert (
        client.post(
            "/api/v1/ingest/apple-health",
            headers={"X-Ingest-Token": "wrong-token"},
            json={"data": {}},
        ).status_code
        == 401
    )


def test_apple_health_min_avg_max_multi_device_and_dedupe(client, db_conn):
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "heart_rate",
                    "units": "count/min",
                    "data": [
                        {
                            "date": "2026-11-14 06:30:00 -0800",
                            "Min": 55,
                            "Avg": 62.5,
                            "Max": 71,
                            "source": "Apple Watch",
                        }
                    ],
                },
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"date": "2026-11-14 06:00:00 -0800", "qty": 120, "source": "iPhone"},
                        {"date": "2026-11-14 06:00:00 -0800", "qty": 340, "source": "Apple Watch"},
                    ],
                },
            ],
            "workouts": [
                {
                    "name": "Cycling",
                    "start": "2026-11-14 09:00:00 -0800",
                    "duration": 2400,
                    "activeEnergyBurned": {"qty": 410},
                    "source": {"name": "Apple Watch", "identifier": "com.apple.health.123"},
                }
            ],
        }
    }

    r1 = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "hemin-secret"},
        json=payload,
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "ok"
    assert r1.json()["metrics_processed"] == 3
    assert r1.json()["workouts_processed"] == 1

    # Verify heart_rate stored non-NULL values and step_count kept both devices
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT value, value_min, value_max, canonical_metric FROM biomarkers WHERE metric_name = 'heart_rate'"
        )
        hr = cur.fetchone()
        assert hr == (62.5, 55.0, 71.0, "heart_rate")

        cur.execute(
            "SELECT device, value FROM biomarkers WHERE metric_name = 'step_count' ORDER BY device"
        )
        assert cur.fetchall() == [("Apple Watch", 340.0), ("iPhone", 120.0)]

    # Exact re-send is deduplicated by SHA-256
    r2 = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "hemin-secret"},
        json=payload,
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"

    # Stats with device filter
    watch_stats = client.get(
        "/api/v1/stats?device=Apple Watch",
        headers={"X-Ingest-Token": "hemin-secret"},
    )
    assert watch_stats.status_code == 200
    body = watch_stats.json()
    assert len(body["biomarkers"]) == 2
    assert len(body["workouts"]) == 1
    assert body["raw_ingestions"][0]["status"] == "processed"


def test_raw_preserved_on_sos_and_reprocess(client, db_conn):
    bad_payload = {"unexpected": "shape"}
    r = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "hemin-secret"},
        json=bad_payload,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "partial_sos"
    assert r.json()["sos_count"] == 1
    raw_id = r.json()["raw_ingestion_id"]

    # Status endpoint reflects critical SOS
    st = client.get("/api/v1/status").json()
    assert st["status"] == "critical"
    assert st["components"]["ingestion"]["open_sos"] == 1
    assert st["components"]["processing"]["failed_raw_ingestions"] == 1

    # Fix raw payload in DB (simulating a parser update) and reprocess
    with db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE raw_ingestions
            SET payload = '{"data": {"metrics": [{"name": "heart_rate", "units": "bpm", "data": [{"date": "2026-11-14 08:00:00 -0800", "Avg": 60}]}]}}'::jsonb
            WHERE id = %s
            """,
            (raw_id,),
        )
    db_conn.commit()

    rep = client.post(
        "/api/v1/admin/reprocess",
        headers={"X-Admin-Token": "admin-secret"},
        json={"raw_ingestion_id": raw_id},
    )
    assert rep.status_code == 200
    assert rep.json()["reprocessed_count"] == 1
    assert rep.json()["results"][0]["metrics_processed"] == 1
    assert rep.json()["results"][0]["sos_count"] == 0

    st2 = client.get("/api/v1/status").json()
    assert st2["components"]["ingestion"]["open_sos"] == 0
    assert st2["components"]["processing"]["failed_raw_ingestions"] == 0


def test_multi_user_isolation_and_bootstrap_does_not_overwrite(client, db_conn):
    create_resp = client.post(
        "/api/v1/users",
        headers={"X-Admin-Token": "admin-secret"},
        json={
            "slug": "alex",
            "display_name": "Alex",
            "ingest_token": "alex-secret",
            "timezone": "America/Los_Angeles",
        },
    )
    assert create_resp.status_code == 200
    alex_id = create_resp.json()["user_id"]

    # Rotate alex's token via POST /users, then run bootstrap_users with old token -> must NOT overwrite
    client.post(
        "/api/v1/users",
        headers={"X-Admin-Token": "admin-secret"},
        json={
            "slug": "alex",
            "display_name": "Alex",
            "ingest_token": "alex-rotated",
            "timezone": "America/Los_Angeles",
        },
    )
    # Verify fresh bootstrap_users seeded hemin's default profile while preserving rotated tokens
    bootstrap_users(db_conn, [("hemin", "hemin-secret"), ("alex", "alex-old-ignored")])
    with db_conn.cursor() as cur:
        cur.execute("SELECT ingest_token_hash FROM users WHERE slug = 'alex'")
        assert cur.fetchone()[0] == hash_token("alex-rotated")
        cur.execute("SELECT date_of_birth::text, sex, sleep_target_hours FROM users WHERE slug = 'hemin'")
        assert cur.fetchone() == ("1992-06-04", "male", 7.5)

    oura_resp = client.post(
        "/api/v1/ingest/points",
        headers={"X-Ingest-Token": "alex-rotated"},
        json={
            "source": "oura_ble",
            "points": [
                {
                    "metric_name": "resting_heart_rate",
                    "recorded_at": "2026-11-14T06:00:00-08:00",
                    "value": 51.0,
                    "unit": "bpm",
                    "device": "Oura Ring 4",
                }
            ],
        },
    )
    assert oura_resp.status_code == 200
    assert oura_resp.json()["user_id"] == alex_id

    # Hemin's stats must NOT include Alex's data
    hemin_stats = client.get("/api/v1/stats", headers={"X-Ingest-Token": "hemin-secret"}).json()
    assert hemin_stats["biomarkers"] == []

    alex_stats = client.get("/api/v1/stats", headers={"X-Ingest-Token": "alex-rotated"}).json()
    assert len(alex_stats["biomarkers"]) == 1
    assert alex_stats["biomarkers"][0]["device"] == "Oura Ring 4"
    assert alex_stats["raw_ingestions"][0]["count"] == 1


def test_payload_size_limit_returns_413(client):
    resp = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "hemin-secret", "Content-Length": str(10 * 1024 * 1024)},
        json={"data": {}},
    )
    assert resp.status_code == 413


def test_oura_ble_ingest_contract_sos_and_reprocess(client, db_conn):
    payload = {
        "device": "Oura Ring 4",
        "events": [
            {"event_type": "hrv", "timestamp": "2026-11-14T06:10:00+00:00", "rmssd": 51.2},
            {"event_type": "spo2", "timestamp": "2026-11-14T06:15:00+00:00", "spo2": 98.5},
        ],
    }
    r = client.post(
        "/api/v1/ingest/oura",
        headers={"X-Ingest-Token": "hemin-secret"},
        json=payload,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["points_processed"] == 2

    # Duplicate payload is skipped
    r_dup = client.post(
        "/api/v1/ingest/oura",
        headers={"X-Ingest-Token": "hemin-secret"},
        json=payload,
    )
    assert r_dup.status_code == 200
    assert r_dup.json()["status"] == "duplicate"

    # Bad shape triggers SOS and failed raw_ingestion, recoverable via reprocess
    r_bad = client.post(
        "/api/v1/ingest/oura",
        headers={"X-Ingest-Token": "hemin-secret"},
        json={"unexpected": "shape"},
    )
    assert r_bad.status_code == 200
    assert r_bad.json()["status"] == "partial_sos"
    assert r_bad.json()["sos_count"] == 1
    raw_id = r_bad.json()["raw_ingestion_id"]

    with db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE raw_ingestions
            SET payload = '{"device": "Oura Ring 4", "events": [{"event_type": "resting_heart_rate", "timestamp": "2026-11-14T06:20:00+00:00", "rhr": 49}]}'::jsonb
            WHERE id = %s
            """,
            (raw_id,),
        )
    db_conn.commit()

    rep = client.post(
        "/api/v1/admin/reprocess",
        headers={"X-Admin-Token": "admin-secret"},
        json={"raw_ingestion_id": raw_id},
    )
    assert rep.status_code == 200
    assert rep.json()["reprocessed_count"] == 1
    assert rep.json()["results"][0]["points_processed"] == 1
    assert rep.json()["results"][0]["sos_count"] == 0

    stats = client.get(
        "/api/v1/stats?source=oura_ble",
        headers={"X-Ingest-Token": "hemin-secret"},
    )
    assert stats.status_code == 200
    metrics = {b["metric_name"]: b for b in stats.json()["biomarkers"]}
    assert metrics["hrv_rmssd"]["canonical_metric"] == "hrv_rmssd"
    assert metrics["hrv_rmssd"]["device"] == "Oura Ring 4"
    assert metrics["spo2"]["canonical_metric"] == "spo2"
    assert metrics["resting_heart_rate"]["canonical_metric"] == "resting_heart_rate"


def test_analysis_rollup_insights_and_briefings_flow(client, db_conn):
    # Configure user profile with DOB (1992-06-04), sex='male', and sleep target 7.5h
    u_resp = client.post(
        "/api/v1/users",
        headers={"X-Admin-Token": "admin-secret"},
        json={
            "slug": "hemin",
            "display_name": "Hemin",
            "ingest_token": "hemin-secret",
            "timezone": "America/Los_Angeles",
            "date_of_birth": "1992-06-04",
            "sex": "male",
            "sleep_target_hours": 7.5,
        },
    )
    assert u_resp.status_code == 200
    uid = u_resp.json()["user_id"]

    # Second isolated user
    u2_resp = client.post(
        "/api/v1/users",
        headers={"X-Admin-Token": "admin-secret"},
        json={
            "slug": "guest",
            "display_name": "Guest",
            "ingest_token": "guest-secret",
            "timezone": "America/Los_Angeles",
        },
    )
    assert u2_resp.status_code == 200

    la_tz = ZoneInfo("America/Los_Angeles")
    today = datetime.now(la_tz).date()

    # Seed 18 days of biomarkers with missing sleep/steps on offsets 4 & 8 (must NOT count toward debt/averages)
    missing_offsets = {4, 8}
    bio_inserts = []
    workout_inserts = []
    for offset in range(17, -1, -1):
        d = today - timedelta(days=offset)
        morning_dt = datetime.combine(d, datetime.min.time(), tzinfo=la_tz) + timedelta(hours=7)
        bed_dt = morning_dt - timedelta(hours=8)  # 23:00 prior evening -> +6h assigns to wake-up day d

        # Baseline normal RHR/HRV until offset=0 when acute strain triggers Rules A & B
        hrv_val = 30.0 if offset == 0 else 58.0 + (offset % 4)
        rhr_val = 66.0 if offset == 0 else 53.0 + (offset % 3)
        resp_val = 18.2 if offset == 0 else 14.5 + (offset % 2) * 0.2
        temp_val = 0.72 if offset == 0 else 0.05

        bio_inserts.extend(
            [
                (uid, "apple_health", "Apple Watch", "heart_rate_variability", "hrv_sdnn", morning_dt, hrv_val, "ms"),
                (uid, "apple_health", "Apple Watch", "resting_heart_rate", "resting_heart_rate", morning_dt, rhr_val, "bpm"),
                (uid, "apple_health", "Apple Watch", "respiratory_rate", "respiratory_rate", morning_dt, resp_val, "breaths/min"),
                (
                    uid,
                    "apple_health",
                    "Apple Watch",
                    "apple_sleeping_wrist_temperature",
                    "skin_temperature_delta",
                    morning_dt,
                    temp_val,
                    "degC",
                ),
            ]
        )

        if offset not in missing_offsets:
            # 7-day avg steps < 5,000 triggers Rule F (sedentary_activity)
            steps_val = 3500.0 + offset * 200.0
            bio_inserts.append((uid, "apple_health", "Apple Watch", "step_count", "steps", morning_dt + timedelta(hours=5), steps_val, "count"))
            # Apple Watch sleeps 7.0h normally, but 6.0h on latest night vs Oura 7.6h -> triggers Rule H gap!
            watch_hr = 6.0 if offset == 0 else 7.0
            oura_hr = 7.6 if offset == 0 else 7.1
            bio_inserts.append((uid, "apple_health", "Apple Watch", "sleep_analysis", "sleep_duration", bed_dt, watch_hr, "hr"))
            # Oura has an overarching 8.0h InBed container + finer stages summing to oura_hr
            bio_inserts.extend(
                [
                    (uid, "oura_ble", "Oura Ring 4", "sleep_duration", "sleep_duration", bed_dt, 8.0, "hr"),
                    (uid, "oura_ble", "Oura Ring 4", "sleep_duration", "sleep_duration", bed_dt + timedelta(minutes=10), 4.0, "hr"),
                    (uid, "oura_ble", "Oura Ring 4", "sleep_duration", "sleep_duration", bed_dt + timedelta(hours=4, minutes=10), oura_hr - 4.0, "hr"),
                ]
            )
            # Moderate chronic workouts (offsets 17..3) followed by high-load ramp (offsets 2..0) -> ACWR > 1.5 (Rule E)
            dur_s = 6000.0 if offset <= 2 else 1800.0
            cal_val = 1050.0 if offset <= 2 else 200.0
            workout_inserts.append(
                (uid, "apple_health", "Apple Watch", "Outdoor Run", morning_dt + timedelta(hours=10), dur_s, cal_val)
            )

    bio_inserts.append((uid, "apple_health", "Apple Watch", "vo2_max", "vo2_max", datetime.combine(today, datetime.min.time(), tzinfo=la_tz) + timedelta(hours=9), 46.0, "ml/kg/min"))

    with db_conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO biomarkers (user_id, source, device, metric_name, canonical_metric, recorded_at, value, unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            bio_inserts,
        )
        cur.executemany(
            """
            INSERT INTO workouts (user_id, source, device, workout_type, started_at, duration_sec, calories)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            workout_inserts,
        )
    db_conn.commit()

    # Trigger rollup + insights + both morning/evening briefings
    run_resp = client.post(
        "/api/v1/analysis/run",
        headers={"X-Ingest-Token": "hemin-secret"},
        json={"days_back": 30, "period": "both", "day": str(today)},
    )
    assert run_resp.status_code == 200
    run_data = run_resp.json()
    assert run_data["status"] == "ok"
    assert run_data["rollup"]["latest_vo2_max"] == 46.0
    assert len(run_data["briefings"]) == 2
    periods = {b["period"]: b for b in run_data["briefings"]}
    assert periods["morning"]["model_used"] == "deterministic"
    assert periods["evening"]["model_used"] == "deterministic"

    # Missing nights (offsets 4 & 8) must have NO sleep_duration or sleep_debt row in daily_metrics
    with db_conn.cursor() as cur:
        for mo in missing_offsets:
            cur.execute(
                "SELECT COUNT(*) FROM daily_metrics WHERE user_id = %s AND day = %s AND canonical_metric IN ('sleep_duration', 'sleep_debt')",
                (uid, today - timedelta(days=mo)),
            )
            assert cur.fetchone()[0] == 0

        # Apple Watch and Oura sleep must remain stored separately without container double-counting
        cur.execute(
            """
            SELECT source, device, value
            FROM daily_metrics
            WHERE user_id = %s AND day = %s AND canonical_metric = 'sleep_duration'
            ORDER BY source
            """,
            (uid, today),
        )
        sleep_rows = cur.fetchall()
        assert len(sleep_rows) == 2
        sleep_by_src = {r[0]: r[2] for r in sleep_rows}
        assert sleep_by_src["apple_health"] == 6.0
        assert sleep_by_src["oura_ble"] == 7.6  # 8h InBed container dropped cleanly!

    insights_resp = client.get(
        "/api/v1/insights?days=14",
        headers={"X-Ingest-Token": "hemin-secret"},
    )
    assert insights_resp.status_code == 200
    body = insights_resp.json()
    assert body["profile"]["date_of_birth"] == "1992-06-04"
    assert body["profile"]["sex"] == "male"
    assert body["profile"]["latest_vo2_max"] == 46.0
    # Within the 14d window (offsets 13..0), 2 nights (offsets 8 & 4) are missing and skipped:
    # 11 nights ~ (7.0 + 7.1)/2 = 7.05h -> deficit 0.45h * 11 = 4.95h;
    # latest night Watch 6.0h & Oura 7.6h -> mean 6.8h -> deficit 0.70h -> total 5.65h!
    assert body["profile"]["sleep_debt_hours"] == 5.65

    rules_found = {ins["rule_id"]: ins for ins in body["insights"]}
    assert "recovery_autonomic" in rules_found
    assert rules_found["recovery_autonomic"]["severity"] == "alert"
    assert "illness_early_warning" in rules_found
    assert rules_found["illness_early_warning"]["severity"] == "alert"
    assert "cardiorespiratory_vo2max" in rules_found
    assert "Excellent" in rules_found["cardiorespiratory_vo2max"]["title"]
    assert "sleep_debt_tracker" in rules_found
    assert "training_load_acwr" in rules_found
    assert rules_found["training_load_acwr"]["severity"] == "alert"
    assert "sedentary_activity" in rules_found
    assert rules_found["sedentary_activity"]["severity"] == "watch"
    assert "personal_correlations" in rules_found
    assert "cross_device_sleep_gap" in rules_found
    assert rules_found["cross_device_sleep_gap"]["severity"] == "watch"

    # Recency guard on Rule H: advancing 5 days ahead without new sleep must NOT fire old gap alert
    future_run = client.post(
        "/api/v1/analysis/run",
        headers={"X-Ingest-Token": "hemin-secret"},
        json={"days_back": 30, "period": "morning", "day": str(today + timedelta(days=5))},
    )
    assert future_run.status_code == 200
    future_rules = {ins["rule_id"] for ins in future_run.json()["insights"]}
    assert "cross_device_sleep_gap" not in future_rules

    # Existing Gemini briefing must NOT be overwritten by a subsequent deterministic fallback
    from prana.analyzer.briefing import generate_briefing
    from prana.config import Settings

    with db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE briefings
            SET model_used = 'gemini-3.5-flash-lite',
                title = 'AI Preserved Evening Briefing'
            WHERE user_id = %s AND day = %s AND period = 'evening'
            """,
            (uid, today),
        )
    db_conn.commit()
    cfg_no_ai = Settings(
        database_url=client.app.state.settings.database_url,
        admin_token=client.app.state.settings.admin_token,
        bootstrap_users=client.app.state.settings.bootstrap_users,
        backup_dir=client.app.state.settings.backup_dir,
        backup_cron_hour=client.app.state.settings.backup_cron_hour,
        backup_retention_days=client.app.state.settings.backup_retention_days,
        timezone="America/Los_Angeles",
        gemini_api_key="",
        gemini_model="gemini-3.5-flash-lite",
    )
    preserved = generate_briefing(db_conn, uid, "evening", cfg_no_ai, day=today)
    db_conn.commit()
    assert preserved["model_used"] == "gemini-3.5-flash-lite"
    assert preserved["title"] == "AI Preserved Evening Briefing"
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT model_used, title FROM briefings WHERE user_id = %s AND day = %s AND period = 'evening'",
            (uid, today),
        )
        b_row = cur.fetchone()
        assert b_row == ("gemini-3.5-flash-lite", "AI Preserved Evening Briefing")

    # Multi-user isolation: guest sees zero insights and zero briefings
    guest_insights = client.get(
        "/api/v1/insights",
        headers={"X-Ingest-Token": "guest-secret"},
    )
    assert guest_insights.status_code == 200
    assert guest_insights.json()["insights"] == []
    assert guest_insights.json()["briefings"] == []
    assert guest_insights.json()["profile"]["sleep_debt_hours"] is None


def test_ingest_auto_refreshes_metrics_and_runs_ai_once_for_morning_slot(client, db_conn, monkeypatch):
    from prana.analyzer.briefing import generate_briefing
    from prana.config import Settings

    la_tz = ZoneInfo("America/Los_Angeles")
    today = datetime.now(la_tz).date()
    morning_ts = datetime.combine(today, datetime.min.time(), tzinfo=la_tz) + timedelta(hours=2)

    gemini_calls = []

    def _fake_gemini(snapshot, settings):
        gemini_calls.append(snapshot.get("day"))
        return {
            "title": f"AI Morning Briefing ({snapshot.get('day')})",
            "summary_markdown": "- Sleep recorded and analyzed once.",
            "action_items": ["Maintain routine"],
            "model_used": settings.gemini_model,
        }

    monkeypatch.setattr("prana.analyzer.briefing._call_gemini_api", _fake_gemini)
    test_cfg = Settings(
        database_url=client.app.state.settings.database_url,
        admin_token=client.app.state.settings.admin_token,
        bootstrap_users=client.app.state.settings.bootstrap_users,
        backup_dir=client.app.state.settings.backup_dir,
        backup_cron_hour=client.app.state.settings.backup_cron_hour,
        backup_retention_days=client.app.state.settings.backup_retention_days,
        timezone="America/Los_Angeles",
        gemini_api_key="fake-test-key",
        gemini_model="gemini-3.5-flash-lite",
        morning_briefing_hour=0,
        evening_briefing_hour=24,
    )
    monkeypatch.setattr(client.app.state, "settings", test_cfg)

    # 1. Scheduled morning run before sleep arrives skips LLM and saves the slot
    pre_sleep = generate_briefing(
        db_conn, 1, "morning", test_cfg, day=today, skip_llm_if_no_sleep=True
    )
    db_conn.commit()
    assert pre_sleep["model_used"] == "deterministic"
    assert len(gemini_calls) == 0

    # 2. Ingesting daytime steps updates daily_metrics immediately, without triggering Gemini
    r_steps = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "hemin-secret"},
        json={
            "data": {
                "metrics": [
                    {
                        "name": "step_count",
                        "units": "count",
                        "data": [
                            {
                                "date": (morning_ts + timedelta(hours=7)).isoformat(),
                                "qty": 1200,
                                "source": "iPhone",
                            }
                        ],
                    }
                ],
                "workouts": [],
            }
        },
    )
    assert r_steps.status_code == 200
    assert len(gemini_calls) == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM daily_metrics WHERE user_id = 1 AND day = %s AND canonical_metric = 'steps'",
            (today,),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 1200.0

    # 3. Ingesting today's sleep refreshes daily_metrics AND triggers the morning AI slot ONCE
    r_sleep = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "hemin-secret"},
        json={
            "data": {
                "metrics": [
                    {
                        "name": "sleep_analysis",
                        "units": "hr",
                        "data": [
                            {
                                "date": morning_ts.isoformat(),
                                "qty": 7.8,
                                "source": "Apple Watch",
                            }
                        ],
                    }
                ],
                "workouts": [],
            }
        },
    )
    assert r_sleep.status_code == 200
    assert len(gemini_calls) == 1

    # 4. Subsequent daytime wearable syncs refresh daily_metrics but do NOT re-invoke Gemini
    r_oura = client.post(
        "/api/v1/ingest/oura",
        headers={"X-Ingest-Token": "hemin-secret"},
        json={
            "device": "Oura Ring 4",
            "events": [
                {
                    "event_type": "resting_heart_rate",
                    "timestamp": (morning_ts + timedelta(hours=8)).isoformat(),
                    "rhr": 58.0,
                }
            ],
        },
    )
    assert r_oura.status_code == 200
    assert len(gemini_calls) == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT model_used FROM briefings WHERE user_id = 1 AND day = %s AND period = 'morning'",
            (today,),
        )
        assert cur.fetchone()[0] == "gemini-3.5-flash-lite"
