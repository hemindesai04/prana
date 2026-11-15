def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ingest_requires_valid_token(client):
    resp = client.post("/api/v1/ingest/apple-health", json={"data": {}})
    assert resp.status_code == 401

    resp_bad = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "wrong-token"},
        json={"data": {}},
    )
    assert resp_bad.status_code == 401


def test_apple_health_ingest_idempotent_and_stats_flow(client, fake_state):
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "heart_rate_variability",
                    "units": "ms",
                    "data": [
                        {"date": "2026-11-14 06:30:00 -0800", "qty": 52.4},
                        {"date": "2026-11-14 07:30:00 -0800", "qty": 58.1},
                    ],
                }
            ],
            "workouts": [
                {
                    "name": "Cycling",
                    "start": "2026-11-14 09:00:00 -0800",
                    "duration": 2400,
                    "activeEnergyBurned": {"qty": 410},
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
    assert r1.json()["metrics_processed"] == 2
    assert r1.json()["workouts_processed"] == 1

    # Re-sending same payload should upsert cleanly without duplicating biomarkers
    r2 = client.post(
        "/api/v1/ingest/apple-health",
        headers={"X-Ingest-Token": "hemin-secret"},
        json=payload,
    )
    assert r2.status_code == 200
    assert len(fake_state.biomarkers) == 2
    assert len(fake_state.workouts) == 1
    assert len(fake_state.raw_ingestions) == 2

    stats = client.get("/api/v1/stats", headers={"X-Ingest-Token": "hemin-secret"})
    assert stats.status_code == 200
    assert stats.json()["user_id"] == 1
    assert stats.json()["biomarkers"][0]["metric_name"] == "heart_rate_variability"
    assert stats.json()["biomarkers"][0]["count"] == 2


def test_multi_user_isolation_and_generic_points_flow(client, fake_state):
    # Admin creates second household user
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
    assert alex_id == 2

    # Alex pushes Oura BLE points
    oura_resp = client.post(
        "/api/v1/ingest/points",
        headers={"X-Ingest-Token": "alex-secret"},
        json={
            "source": "oura_ble",
            "points": [
                {
                    "metric_name": "resting_heart_rate",
                    "recorded_at": "2026-11-14T06:00:00-08:00",
                    "value": 51.0,
                    "unit": "bpm",
                }
            ],
        },
    )
    assert oura_resp.status_code == 200
    assert oura_resp.json()["user_id"] == alex_id

    # Hemin's stats must NOT include Alex's data
    hemin_stats = client.get("/api/v1/stats", headers={"X-Ingest-Token": "hemin-secret"})
    assert hemin_stats.status_code == 200
    assert hemin_stats.json()["biomarkers"] == []

    # Alex's stats include only Alex's data
    alex_stats = client.get("/api/v1/stats", headers={"X-Admin-Token": "unused", "X-Ingest-Token": "alex-secret"})
    assert alex_stats.status_code == 200
    assert len(alex_stats.json()["biomarkers"]) == 1
    assert alex_stats.json()["biomarkers"][0]["source"] == "oura_ble"
