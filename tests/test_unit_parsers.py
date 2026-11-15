import json
import logging
from zoneinfo import ZoneInfo

import pytest

from prana.config import parse_bootstrap_users
from prana.db import compute_payload_sha256, hash_token, sanitize_json_value
from prana.ingestor.apple_health import (
    extract_numeric,
    parse_apple_health_payload,
    parse_timestamp,
)
from prana.ingestor.registry import resolve_canonical_metric
from prana.logging_setup import JsonFormatter


def test_parse_bootstrap_users_and_duplicates():
    parsed = parse_bootstrap_users("hemin:tok1, alex:tok2 , invalid, :missing")
    assert parsed == [("hemin", "tok1"), ("alex", "tok2")]

    with pytest.raises(ValueError, match="Duplicate ingest token"):
        parse_bootstrap_users("hemin:same, alex:same")

    with pytest.raises(ValueError, match="Duplicate slug"):
        parse_bootstrap_users("hemin:tok1, hemin:tok2")


def test_hash_and_sha256_and_sanitize():
    digest = hash_token("my-secret-token")
    assert len(digest) == 64
    assert "my-secret-token" not in digest

    sha1 = compute_payload_sha256({"b": 2, "a": 1})
    sha2 = compute_payload_sha256({"a": 1, "b": 2})
    assert sha1 == sha2

    cleaned = sanitize_json_value({"k\x00": "hel\x00lo", "nested": ["a\x00b"]})
    assert cleaned == {"k": "hello", "nested": ["ab"]}


def test_parse_timestamp_formats_and_user_tz():
    dt_hae = parse_timestamp("2026-11-14 07:15:00 -0800")
    dt_iso = parse_timestamp("2026-11-14T07:15:00-08:00")
    assert dt_hae == dt_iso

    dt_naive = parse_timestamp("2026-11-14 07:15:00", user_tz="America/Los_Angeles")
    assert dt_naive.tzinfo == ZoneInfo("America/Los_Angeles")


def test_parse_timestamp_invalid_raises():
    with pytest.raises(ValueError):
        parse_timestamp("not-a-timestamp")


def test_extract_numeric_case_insensitive_and_bool_guard():
    assert extract_numeric({"qty": 72, "avg": 70}) == 72.0
    assert extract_numeric({"Min": 55, "Avg": 62.5, "Max": 71}) == 62.5
    assert extract_numeric({"qty": True}) is None


def test_parse_apple_health_realistic_hae_payload():
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
                            "start": "2026-11-14 06:30:00 -0800",
                            "end": "2026-11-14 06:30:00 -0800",
                            "context": "sedentary",
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
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {
                            "date": "2026-11-14 00:00:00 -0800",
                            "totalSleep": 7.1,
                            "asleep": 0,
                            "core": 4.0,
                            "deep": 1.2,
                            "rem": 1.9,
                            "source": "Apple Watch",
                        }
                    ],
                },
            ],
            "workouts": [
                {
                    "name": "Outdoor Run",
                    "start": "2026-11-14 07:00:00 -0800",
                    "end": "2026-11-14 07:35:00 -0800",
                    "duration": 2100,
                    "activeEnergyBurned": {"qty": 350.5, "units": "kcal"},
                    "source": {"name": "Apple Watch", "identifier": "com.apple.health.123"},
                }
            ],
        }
    }

    points, workouts, issues = parse_apple_health_payload(payload, user_tz="America/Los_Angeles")
    assert issues == []
    assert len(points) == 4
    assert points[0].metric_name == "heart_rate"
    assert points[0].canonical_metric == "heart_rate"
    assert points[0].value == 62.5
    assert points[0].value_min == 55.0
    assert points[0].value_max == 71.0
    assert points[0].device == "Apple Watch"

    assert points[1].device == "iPhone"
    assert points[2].device == "Apple Watch"
    assert points[3].value == 7.1

    assert len(workouts) == 1
    assert workouts[0].device == "Apple Watch"
    assert workouts[0].calories == 350.5


def test_parse_apple_health_drift_and_sos():
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "brand_new_metric",
                    "units": "units",
                    "data": [
                        {"date": "2026-11-14 06:00:00 -0800", "qty": 10, "newField": "x"},
                        {"date": "bad-date", "qty": 10},
                        {"date": "2026-11-14 06:05:00 -0800"},
                    ],
                }
            ]
        }
    }
    points, _, issues = parse_apple_health_payload(payload)
    assert len(points) == 1
    assert points[0].canonical_metric is None
    assert points[0].metadata["newField"] == "x"
    severities = {(i.severity, i.issue_type) for i in issues}
    assert ("warning", "unknown_metric") in severities
    assert ("warning", "unknown_entry_keys") in severities
    assert ("sos", "unparseable_timestamp") in severities
    assert ("sos", "no_usable_value") in severities


def test_canonical_registry():
    assert resolve_canonical_metric("apple_health", "heart_rate_variability") == ("hrv_sdnn", "ms")
    assert resolve_canonical_metric("apple_health", "apple_exercise_time") == ("exercise_minutes", "min")
    assert resolve_canonical_metric("oura_ble", "hrv") == ("hrv_rmssd", "ms")
    assert resolve_canonical_metric("apple_health", "unknown_x") is None


def test_json_formatter_outputs_valid_json_with_request_id():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="prana.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.user_id = 1
    record.request_id = "req-123"
    parsed = json.loads(formatter.format(record))
    assert parsed["message"] == "hello world"
    assert parsed["user_id"] == 1
    assert parsed["request_id"] == "req-123"
