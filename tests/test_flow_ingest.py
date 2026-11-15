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
    bootstrap_users(db_conn, [("alex", "alex-old-ignored")])
    with db_conn.cursor() as cur:
        cur.execute("SELECT ingest_token_hash FROM users WHERE slug = 'alex'")
        assert cur.fetchone()[0] == hash_token("alex-rotated")

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
