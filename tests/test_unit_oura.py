import importlib.util
from pathlib import Path
import sqlite3

from prana.ingestor.oura import parse_oura_payload


def test_parse_oura_payload_normalizes_all_core_metrics():
    payload = {
        "device": "Oura Ring 4",
        "events": [
            {"event_type": "heart_rate", "timestamp": "2026-11-14T06:00:00+00:00", "bpm": 62},
            {"event_type": "resting_heart_rate", "timestamp": "2026-11-14T06:05:00+00:00", "rhr": 50},
            {"event_type": "hrv", "timestamp": "2026-11-14T06:10:00+00:00", "rmssd": 48.5},
            {"event_type": "spo2", "timestamp": "2026-11-14T06:15:00+00:00", "spo2": 98.0},
            {"event_type": "temperature", "timestamp": "2026-11-14T06:20:00+00:00", "temperature_delta": -0.2},
            {"event_type": "sleep_period", "timestamp": "2026-11-14T07:00:00+00:00", "duration_sec": 25200},
            {"event_type": "steps", "timestamp": 1794643200, "steps": 150},
        ],
    }

    points, issues = parse_oura_payload(payload, user_tz="America/Los_Angeles")
    assert issues == []
    assert len(points) == 7
    by_metric = {p.metric_name: p for p in points}
    assert by_metric["heart_rate"].value == 62.0
    assert by_metric["heart_rate"].canonical_metric == "heart_rate"
    assert by_metric["resting_heart_rate"].value == 50.0
    assert by_metric["hrv_rmssd"].canonical_metric == "hrv_rmssd"
    assert by_metric["spo2"].canonical_metric == "spo2"
    assert by_metric["skin_temperature_delta"].canonical_metric == "skin_temperature_delta"
    assert by_metric["sleep_duration"].value == 7.0
    assert by_metric["steps"].canonical_metric == "steps"
    assert all(p.device == "Oura Ring 4" for p in points)


def test_parse_oura_payload_drift_and_sos():
    bad_shape_pts, bad_shape_issues = parse_oura_payload({"wrong": []})
    assert bad_shape_pts == []
    assert bad_shape_issues[0].severity == "sos"
    assert bad_shape_issues[0].issue_type == "payload_shape"

    payload = {
        "events": [
            {"event_type": "new_oura_metric", "timestamp": "2026-11-14T06:00:00Z", "value": 42, "extraField": 1},
            {"event_type": "hrv", "timestamp": "bad-ts", "rmssd": 50},
            {"event_type": "hrv", "timestamp": "2026-11-14T06:05:00Z"},
        ]
    }
    pts, issues = parse_oura_payload(payload)
    assert len(pts) == 1
    assert pts[0].canonical_metric is None
    severities = {(i.severity, i.issue_type) for i in issues}
    assert ("warning", "unknown_metric") in severities
    assert ("warning", "unknown_entry_keys") in severities
    assert ("sos", "unparseable_timestamp") in severities
    assert ("sos", "no_usable_value") in severities


def test_parse_oura_payload_skips_ring_telemetry_and_parses_real_ring_events():
    payload = {
        "device": "Oura Ring 4",
        "events": [
            {"event_type": "debug_event", "timestamp": 1794714795, "ascii": "blestda;50"},
            {"event_type": "debug_data", "timestamp": 1794714795, "raw": "1910ffff", "kind": "debug_data"},
            {"event_type": "ble_connection", "timestamp": 1794714795, "kind": "ble_connection_ind"},
            {"event_type": "time_sync", "timestamp": 1794714795, "unix_time": 1794713559},
            {"event_type": "ring_start", "timestamp": 1794714795, "firmware_version": "2.12.15"},
            {"event_type": "state_change", "timestamp": 1794717457, "text": "chg. stopped", "state": 1},
            {"event_type": "feature_session", "timestamp": 1794717457, "value": 5, "feature_id": 2},
            {"event_type": "wear_event", "timestamp": 1794717457, "text": "575", "state": 3},
            {"event_type": "scan_start", "timestamp": 1794717457, "raw": "0305000102"},
            {"event_type": "scan_end", "timestamp": 1794717457, "raw": "401e010804"},
            {"event_type": "motion_event", "timestamp": 1794717457, "motion_seconds": 26},
            {"event_type": "motion_period", "timestamp": 1794717458, "motion_levels": [3, 3, 2]},
            {"event_type": "green_ibi_quality_event", "timestamp": 1794717457, "hr_bpm": [], "ibi_ms": [2000]},
            {"event_type": "temp_event", "timestamp": 1794714795, "temps_c": [31.41, 33.0, 26.76]},
            {
                "event_type": "green_ibi_quality_event",
                "timestamp": 1794717457,
                "hr_bpm": [92, 89],
                "ibi_ms": [471, 649],
                "quality": [0, 1],
            },
            {
                "event_type": "activity_information",
                "timestamp": 1794717458,
                "met": [1.9, 1.3, 1.3],
                "state": 28,
            },
        ],
    }
    pts, issues = parse_oura_payload(payload)
    assert issues == []
    assert len(pts) == 3
    by_metric = {p.metric_name: p for p in pts}
    assert by_metric["skin_temperature"].canonical_metric == "skin_temperature"
    assert by_metric["skin_temperature"].unit == "degC"
    assert by_metric["skin_temperature"].value == 30.39
    assert by_metric["skin_temperature"].value_min == 26.76
    assert by_metric["skin_temperature"].value_max == 33.0
    assert by_metric["heart_rate"].canonical_metric == "heart_rate"
    assert by_metric["heart_rate"].value == 90.5
    assert by_metric["heart_rate"].value_min == 89.0
    assert by_metric["heart_rate"].value_max == 92.0
    assert by_metric["activity_met"].canonical_metric == "activity_met"
    assert by_metric["activity_met"].unit == "MET"
    assert by_metric["activity_met"].value == 1.5


def test_oura_sync_sqlite_loader_and_watermark(tmp_path, monkeypatch):
    sync_path = Path("/oura-ble/sync.py")
    if not sync_path.exists():
        sync_path = Path(__file__).resolve().parent.parent / "oura-ble" / "sync.py"
    spec = importlib.util.spec_from_file_location("oura_ble_sync", sync_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db_path = tmp_path / "oura.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, name TEXT, ring_timestamp INTEGER, decoded_json TEXT, captured_unix INTEGER)"
        )
        conn.execute(
            "INSERT INTO events VALUES (1, 'temp_event', 100, '{\"temps_c\": [32.0, 33.0], \"event_type\": \"temp_event\"}', 1794714795)"
        )
        conn.execute("INSERT INTO events VALUES (2, 'bad', 101, '{not-json', 1794714796)")
        conn.execute("INSERT INTO events VALUES (3, 'hr', 102, '{\"heart_rate\": 60}', 1794714797)")
        conn.execute("INSERT INTO events VALUES (4, 'hr', 702, '{\"heart_rate\": 72}', 1794714797)")
        conn.commit()

    events, max_id = mod._load_new_events(db_path, after_id=1)
    assert max_id == 4
    assert len(events) == 2
    assert events[0]["event_type"] == "hr"
    assert events[0]["timestamp"] == 1794714737.0
    assert events[1]["timestamp"] == 1794714797.0

    wm_file = tmp_path / "wm.txt"
    monkeypatch.setattr(mod, "WATERMARK_FILE", wm_file)
    assert mod._read_watermark() == 0
    mod._write_watermark(max_id)
    assert mod._read_watermark() == 4
