import importlib.util
from pathlib import Path
import sqlite3
import subprocess

import httpx
import pytest

from prana.ingestor.oura import parse_oura_payload


def _load_sync_module():
    sync_path = Path("/oura-ble/sync.py")
    if not sync_path.exists():
        sync_path = Path(__file__).resolve().parent.parent / "oura-ble" / "sync.py"
    spec = importlib.util.spec_from_file_location("oura_ble_sync", sync_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    mod = _load_sync_module()

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


def test_parse_oura_payload_real_ring4_sleep_hrv_and_system_events():
    payload = {
        "device": "Oura Ring 4",
        "events": [
            {"event_type": "sleep_acm_period", "timestamp": 1794740170, "acm_mad": [2.9, 0.05]},
            {"event_type": "alert_event", "timestamp": 1794740170, "alert_type": 1},
            {"event_type": "rtc_beacon", "timestamp": 1794732242, "trailer": 502, "unix_time": 1794732242},
            {"event_type": "user_information", "timestamp": 1794732242, "age_years": 40, "sex_code": 2},
            {"event_type": "ibi_and_amplitude_event", "timestamp": 1794740000, "hr_bpm": [], "ibi_ms": [2000]},
            {"event_type": "hrv_event", "timestamp": 1794740000, "rmssd_ms": [], "hr_bpm": []},
            {"event_type": "sleep_temp_event", "timestamp": 1794740000, "temps_c": []},
            {
                "event_type": "bedtime_period",
                "timestamp": 1794740170.2,
                "bedtime_start_ds": 273647,
                "bedtime_end_ds": 601077,
                "duration_hours": 9.1,
            },
            {
                "event_type": "hrv_event",
                "timestamp": 1794745000,
                "hr_bpm": [78],
                "interval_min": 5,
                "rmssd_ms": [39],
            },
            {
                "event_type": "ibi_and_amplitude_event",
                "timestamp": 1794745100,
                "amplitude": [0, 66],
                "hr_bpm": [61, 77],
                "ibi_ms": [972, 778],
            },
            {"event_type": "sleep_temp_event", "timestamp": 1794745200, "temps_c": [34.95, 35.05]},
            {"event_type": "temp_period", "timestamp": 1794745300, "temps_c": [35.45]},
        ],
    }

    pts, issues = parse_oura_payload(payload, user_tz="America/Los_Angeles")
    assert issues == []
    assert len(pts) == 5

    sleep_pt = pts[0]
    assert sleep_pt.metric_name == "sleep_duration"
    assert sleep_pt.canonical_metric == "sleep_duration"
    assert sleep_pt.value == 9.1
    assert sleep_pt.unit == "hr"

    hrv_pt = pts[1]
    assert hrv_pt.metric_name == "hrv_rmssd"
    assert hrv_pt.canonical_metric == "hrv_rmssd"
    assert hrv_pt.value == 39.0
    assert hrv_pt.unit == "ms"

    hr_pt = pts[2]
    assert hr_pt.metric_name == "heart_rate"
    assert hr_pt.canonical_metric == "heart_rate"
    assert hr_pt.value == 69.0
    assert hr_pt.value_min == 61.0
    assert hr_pt.value_max == 77.0

    assert pts[3].metric_name == "skin_temperature"
    assert pts[3].value == 35.0
    assert pts[4].metric_name == "skin_temperature"
    assert pts[4].value == 35.45


def test_oura_sync_global_rtc_anchor_and_bedtime_start_timestamp(tmp_path):
    mod = _load_sync_module()
    db_path = tmp_path / "oura.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, name TEXT, ring_timestamp INTEGER, decoded_json TEXT, captured_unix INTEGER)"
        )
        conn.execute(
            "INSERT INTO events VALUES (1, 'temp_event', 184365, '{\"temps_c\": [32.0]}', 1794775690)"
        )
        conn.execute(
            "INSERT INTO events VALUES (2, 'rtc_beacon', 194365, '{\"unix_time\": 1794732242}', 1794775690)"
        )
        conn.execute(
            "INSERT INTO events VALUES (3, 'bedtime_period', 629881, '{\"bedtime_start_ds\": 273647, \"bedtime_end_ds\": 601077, \"duration_hours\": 9.1}', 1794775697)"
        )
        conn.commit()

    events, max_id = mod._load_new_events(db_path, after_id=0)
    assert max_id == 3
    assert len(events) == 3
    # 10000 ds = 1000s before rtc_beacon
    assert events[0]["timestamp"] == 1794731242.0
    # bedtime_period uses bedtime_start_ds = 273647 (7928.2s after rtc_beacon)
    assert events[2]["timestamp"] == 1794740170.2


def test_oura_sync_parse_scan_rssi():
    mod = _load_sync_module()
    assert mod.parse_scan_rssi("  -68 dBm  Oura Ring 4  (hci0/dev_6B_FC_E5_89_43_CB)") == -68
    assert mod.parse_scan_rssi("  -85 dBm  Oura Ring 4\n  -71 dBm  Oura Ring 4") == -71
    assert mod.parse_scan_rssi("") is None


def test_oura_sync_presence_cooldown_and_failure_does_not_count(tmp_path, monkeypatch):
    mod = _load_sync_module()

    key_file = tmp_path / "key.hex"
    key_file.write_text("00" * 16)
    db_file = tmp_path / "oura.db"
    wm_file = tmp_path / "wm.txt"
    last_sync_file = tmp_path / "last_sync.txt"

    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, name TEXT, ring_timestamp INTEGER, decoded_json TEXT, captured_unix INTEGER)"
        )
        conn.commit()

    monkeypatch.setattr(mod, "KEY_FILE", key_file)
    monkeypatch.setattr(mod, "DB_FILE", db_file)
    monkeypatch.setattr(mod, "WATERMARK_FILE", wm_file)
    monkeypatch.setattr(mod, "LAST_SYNC_FILE", last_sync_file)
    monkeypatch.setattr(mod, "OURA_INGEST_TOKEN", "tok-test")
    monkeypatch.setattr(mod, "SYNC_INTERVAL_SEC", 3600)
    monkeypatch.setattr(mod, "MIN_RSSI_DBM", -80)
    monkeypatch.setattr(mod, "BATCH_LIMIT", 2)

    scan_outputs = []
    sync_calls = []
    post_batches = []
    fail_sync = False
    fail_post_on_call = 0

    def fake_run(cmd, **kwargs):
        if "scan" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=scan_outputs.pop(0), stderr="")
        if "sync" in cmd:
            sync_calls.append(cmd)
            if fail_sync:
                raise subprocess.CalledProcessError(1, cmd)
            with sqlite3.connect(str(db_file)) as c:
                for idx in range(1, 4):
                    c.execute(
                        "INSERT OR IGNORE INTO events VALUES (?, 'hrv_event', ?, ?, ?)",
                        (idx, 1000 + idx * 100, '{"rmssd_ms": [40]}', 1794775000 + idx),
                    )
                c.commit()
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected cmd: {cmd}")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok"}

    def fake_post(url, headers=None, json=None, timeout=None):
        post_batches.append(len(json["events"]))
        if fail_post_on_call and len(post_batches) == fail_post_on_call:
            raise httpx.ConnectError("prana unavailable")
        return FakeResp()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.httpx, "post", fake_post)

    # 1. Ring absent / weak signal -> out_of_range, no sync, no timestamp recorded
    scan_outputs.append("")
    assert mod.sync_once(now=10000.0) == "out_of_range"
    assert sync_calls == []
    assert not last_sync_file.exists()

    scan_outputs.append("  -86 dBm  Oura Ring 4  (hci0/dev_1)")
    assert mod.sync_once(now=10030.0) == "out_of_range"
    assert sync_calls == []
    assert not last_sync_file.exists()

    # 2. Ring connected/in range (-68 dBm), but `oura sync` fails -> does NOT count as sync
    scan_outputs.append("  -68 dBm  Oura Ring 4  (hci0/dev_1)")
    fail_sync = True
    with pytest.raises(subprocess.CalledProcessError):
        mod.sync_once(now=10060.0)
    assert len(sync_calls) == 1
    assert not last_sync_file.exists()

    # 3. `oura sync` succeeds (writes 3 events = 2 batches), HTTP fails on batch 2 -> still does NOT count as sync
    scan_outputs.append("  -68 dBm  Oura Ring 4  (hci0/dev_1)")
    fail_sync = False
    fail_post_on_call = 2
    with pytest.raises(httpx.ConnectError):
        mod.sync_once(now=10090.0)
    assert post_batches == [2, 1]
    assert mod._read_watermark() == 2  # batch 1 persisted, batch 2 not marked
    assert not last_sync_file.exists()

    # 4. Retry succeeds: drains remaining batch 2 (1 event) and finishes `oura sync` -> records last_sync_ts
    fail_post_on_call = 0
    scan_outputs.append("  -68 dBm  Oura Ring 4  (hci0/dev_1)")
    assert mod.sync_once(now=10120.0) == "synced"
    assert post_batches == [2, 1, 1]
    assert mod._read_watermark() == 3
    assert mod._read_last_sync_ts() == 10120.0

    # 5. Inside 1-hour cooldown (<3600s) -> does NOT even run `oura scan`
    assert mod.sync_once(now=10120.0 + 1800.0) == "cooldown"
    assert scan_outputs == []

    # 6. After 1-hour cooldown (>=3600s) -> scans again
    scan_outputs.append("")
    assert mod.sync_once(now=10120.0 + 3600.0) == "out_of_range"
    assert scan_outputs == []
