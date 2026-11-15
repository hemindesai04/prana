from datetime import datetime, timedelta, timezone
import io
import json
import urllib.error

from prana.analyzer.briefing import _build_deterministic_briefing, _call_gemini_api
from prana.analyzer.rollup import _normalize_temperature_point, compute_sleep_night_hours
from prana.analyzer.rules import _classify_vo2max, spearman_corr
from prana.config import Settings


def _dt(hour: int, minute: int = 0) -> datetime:
    base = datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(hours=hour, minutes=minute)


def test_sleep_container_removal_and_interval_merging():
    # Single standalone full-night segment must be preserved untouched
    single_hr, single_stats = compute_sleep_night_hours([(_dt(0), 7.5, {})])
    assert single_hr == 7.5
    assert single_stats["dropped_containers"] == 0

    # InBed 8h container wrapping 3.0h + 2.5h + 1.8h finer stages (7.3h total)
    # Plus an explicit Awake segment (0.5h) that must be ignored
    hours, stats = compute_sleep_night_hours(
        [
            (_dt(0, 15), 8.0, {"value": "InBed"}),
            (_dt(0, 20), 3.0, {"value": "AsleepCore"}),
            (_dt(3, 20), 0.5, {"value": "Awake"}),
            (_dt(3, 50), 2.5, {"value": "AsleepDeep"}),
            (_dt(6, 20), 1.8, {"value": "AsleepREM"}),
        ]
    )
    assert hours == 7.3
    assert stats["dropped_containers"] == 1
    assert stats["merged_segments"] == 2

    # Overlapping duplicate segments from two export passes must merge rather than double-count
    merged_hr, _ = compute_sleep_night_hours(
        [
            (_dt(1, 0), 2.0, {}),
            (_dt(2, 0), 2.0, {}),
        ]
    )
    assert merged_hr == 3.0

    # Single-child InBed container wrapping a single unsegmented Asleep block must drop InBed
    single_child_hr, single_child_stats = compute_sleep_night_hours(
        [
            (_dt(0, 0), 8.0, {"value": "InBed"}),
            (_dt(0, 30), 7.0, {"value": "Asleep"}),
        ]
    )
    assert single_child_hr == 7.0
    assert single_child_stats["dropped_containers"] == 1


def test_vo2max_acsm_classification_and_spearman():
    tier_m, bracket_m = _classify_vo2max(46.0, age=34, sex="male")
    assert tier_m.startswith("Excellent")
    assert bracket_m == "Male 30-39"

    tier_f, bracket_f = _classify_vo2max(41.0, age=34, sex="female")
    assert tier_f.startswith("Superior")
    assert bracket_f == "Female 30-39"

    assert spearman_corr([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) == 1.0
    assert spearman_corr([1.0, 2.0, 3.0, 4.0], [40.0, 30.0, 20.0, 10.0]) == -1.0
    assert spearman_corr([1.0, 2.0], [3.0, 4.0]) is None


def test_temperature_degf_and_delta_normalization():
    # Apple Health wrist temp exported in degF (~97.18 degF) must convert to Celsius skin_temperature
    canon, val, _, _, unit = _normalize_temperature_point("skin_temperature_delta", 97.18, None, None, "degF")
    assert canon == "skin_temperature"
    assert unit == "degC"
    assert round(val, 2) == 36.21

    # True relative Celsius delta stays skin_temperature_delta
    canon_d, val_d, _, _, unit_d = _normalize_temperature_point("skin_temperature_delta", 0.65, None, None, "degC")
    assert canon_d == "skin_temperature_delta"
    assert val_d == 0.65
    assert unit_d == "degC"


def test_briefing_gemini_and_deterministic_fallback(monkeypatch):
    cfg_no_key = Settings(
        database_url="postgresql://x",
        bootstrap_users=[],
        admin_token="adm",
        backup_dir="/tmp/backups",
        backup_cron_hour=3,
        backup_retention_days=30,
        timezone="America/Los_Angeles",
        gemini_api_key="",
        gemini_model="gemini-2.5-flash",
    )
    snapshot = {
        "user": {
            "slug": "hemin",
            "display_name": "Hemin",
            "age": 34,
            "sex": "male",
            "sleep_target_hours": 7.5,
            "sleep_debt_hours": None,
            "latest_vo2_max": 45.8,
        },
        "period": "morning",
        "day": "2026-11-15",
        "sleep": {"last_night": None},
        "latest_vitals": {},
        "today_activity": None,
        "workouts_today": [],
        "insights": [],
    }
    assert _call_gemini_api(snapshot, cfg_no_key) is None
    det = _build_deterministic_briefing(snapshot)
    assert det["model_used"] == "deterministic"
    assert "Not recorded yet" in det["summary_markdown"]

    cfg_with_key = Settings(
        database_url="postgresql://x",
        bootstrap_users=[],
        admin_token="adm",
        backup_dir="/tmp/backups",
        backup_cron_hour=3,
        backup_retention_days=30,
        timezone="America/Los_Angeles",
        gemini_api_key="test-gemini-key",
        gemini_model="gemini-2.5-flash",
    )

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._buf = io.BytesIO(payload)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return self._buf.read()

    captured_headers = {}

    def _fake_urlopen_ok(req, timeout=20):
        captured_headers.update(dict(req.header_items()))
        reply = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "title": "Solid Readiness — Go Zone 2 + Strength",
                                        "summary_markdown": "- Sleep not yet synced today.\n- VO2max 45.8 ml/kg/min (Excellent).",
                                        "action_items": ["Take 15 min morning light", "Hit 45m Zone 2"],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        return _FakeResponse(json.dumps(reply).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_ok)
    gem = _call_gemini_api(snapshot, cfg_with_key)
    assert gem is not None
    assert gem["model_used"] == "gemini-2.5-flash"
    assert gem["title"].startswith("Solid Readiness")
    assert captured_headers.get("X-goog-api-key") == "test-gemini-key"

    def _fake_urlopen_err(_req, timeout=20):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_err)
    assert _call_gemini_api(snapshot, cfg_with_key) is None
