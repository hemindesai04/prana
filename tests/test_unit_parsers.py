import json
import logging

import pytest

from prana.config import parse_bootstrap_users
from prana.db import hash_token
from prana.ingestor.apple_health import (
    extract_numeric,
    parse_apple_health_payload,
    parse_timestamp,
)
from prana.logging_setup import JsonFormatter


def test_parse_bootstrap_users():
    parsed = parse_bootstrap_users("hemin:tok1, alex:tok2 , invalid, :missing")
    assert parsed == [("hemin", "tok1"), ("alex", "tok2")]


def test_hash_token_is_deterministic_and_non_plaintext():
    digest = hash_token("my-secret-token")
    assert len(digest) == 64
    assert digest == hash_token("my-secret-token")
    assert "my-secret-token" not in digest


def test_parse_timestamp_formats():
    dt_hae = parse_timestamp("2026-11-14 07:15:00 -0800")
    dt_iso = parse_timestamp("2026-11-14T07:15:00-08:00")
    assert dt_hae == dt_iso


def test_parse_timestamp_invalid_raises():
    with pytest.raises(ValueError):
        parse_timestamp("not-a-timestamp")


def test_extract_numeric_priority_and_bool_guard():
    assert extract_numeric({"qty": 72, "avg": 70}) == 72.0
    assert extract_numeric({"avg": 65.5}) == 65.5
    assert extract_numeric({"qty": True}) is None


def test_parse_apple_health_payload_metrics_and_workouts():
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "heart_rate",
                    "units": "count/min",
                    "data": [
                        {
                            "date": "2026-11-14 07:15:00 -0800",
                            "qty": 72,
                            "min": 65,
                            "max": 80,
                            "source": "Apple Watch",
                        },
                        {"date": "bad-date", "qty": 99},
                    ],
                }
            ],
            "workouts": [
                {
                    "name": "Running",
                    "start": "2026-11-14 08:00:00 -0800",
                    "end": "2026-11-14 08:30:00 -0800",
                    "duration": 1800,
                    "activeEnergyBurned": {"qty": 320.5},
                }
            ],
        }
    }
    points, workouts = parse_apple_health_payload(payload)
    assert len(points) == 1
    assert points[0].metric_name == "heart_rate"
    assert points[0].value == 72.0
    assert points[0].value_min == 65.0
    assert points[0].metadata == {"source": "Apple Watch"}

    assert len(workouts) == 1
    assert workouts[0].workout_type == "Running"
    assert workouts[0].calories == 320.5


def test_json_formatter_outputs_valid_json():
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
    parsed = json.loads(formatter.format(record))
    assert parsed["message"] == "hello world"
    assert parsed["user_id"] == 1
    assert parsed["level"] == "INFO"
