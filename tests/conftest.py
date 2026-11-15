from contextlib import contextmanager
from datetime import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from prana.config import Settings
from prana.db import hash_token
from prana.main import create_app


class FakeState:
    """In-memory relational state simulating PostgreSQL tables for flow tests."""

    def __init__(self) -> None:
        self.users: Dict[str, Dict[str, Any]] = {
            "hemin": {
                "id": 1,
                "slug": "hemin",
                "display_name": "Hemin",
                "ingest_token_hash": hash_token("hemin-secret"),
                "timezone": "America/Los_Angeles",
            }
        }
        self.next_user_id = 2
        self.raw_ingestions: List[Dict[str, Any]] = []
        self.biomarkers: Dict[Tuple[int, str, str, datetime], Dict[str, Any]] = {}
        self.workouts: Dict[Tuple[int, str, str, datetime], Dict[str, Any]] = {}


class FakeCursor:
    def __init__(self, state: FakeState) -> None:
        self.state = state
        self._rows: List[Tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        if normalized == "SELECT 1":
            self._rows = [(1,)]
        elif normalized.startswith("SELECT id FROM users WHERE ingest_token_hash"):
            digest = params[0]
            match = next(
                (u["id"] for u in self.state.users.values() if u["ingest_token_hash"] == digest),
                None,
            )
            self._rows = [(match,)] if match is not None else []
        elif normalized.startswith("INSERT INTO users"):
            slug, display_name, token_hash, tz = params
            if slug in self.state.users:
                u = self.state.users[slug]
                u.update({"display_name": display_name, "ingest_token_hash": token_hash, "timezone": tz})
                uid = u["id"]
            else:
                uid = self.state.next_user_id
                self.state.next_user_id += 1
                self.state.users[slug] = {
                    "id": uid,
                    "slug": slug,
                    "display_name": display_name,
                    "ingest_token_hash": token_hash,
                    "timezone": tz,
                }
            self._rows = [(uid,)]
        elif normalized.startswith("INSERT INTO raw_ingestions"):
            user_id, source, payload_json = params
            self.state.raw_ingestions.append(
                {"user_id": user_id, "source": source, "payload": json.loads(payload_json)}
            )
        elif normalized.startswith("INSERT INTO biomarkers"):
            user_id, source, metric_name, recorded_at, val, val_min, val_max, unit, meta = params
            key = (user_id, source, metric_name, recorded_at)
            self.state.biomarkers[key] = {
                "user_id": user_id,
                "source": source,
                "metric_name": metric_name,
                "recorded_at": recorded_at,
                "value": val,
                "value_min": val_min,
                "value_max": val_max,
                "unit": unit,
                "metadata": json.loads(meta),
            }
        elif normalized.startswith("INSERT INTO workouts"):
            user_id, source, w_type, started_at, ended_at, dur, cals, meta = params
            key = (user_id, source, w_type, started_at)
            self.state.workouts[key] = {
                "user_id": user_id,
                "source": source,
                "workout_type": w_type,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_sec": dur,
                "calories": cals,
                "metadata": json.loads(meta),
            }
        elif "FROM biomarkers WHERE user_id = %s" in normalized:
            uid = params[0]
            grouped: Dict[Tuple[str, str], List[datetime]] = {}
            for (u, src, metric, rec_at) in self.state.biomarkers:
                if u == uid:
                    grouped.setdefault((src, metric), []).append(rec_at)
            self._rows = [
                (src, metric, len(times), max(times))
                for (src, metric), times in sorted(grouped.items())
            ]
        else:
            raise AssertionError(f"Unhandled SQL in FakeCursor: {normalized}")

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._rows)


class FakeConnection:
    def __init__(self, state: FakeState) -> None:
        self.state = state
        self.committed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.state)

    def commit(self) -> None:
        self.committed = True


@pytest.fixture()
def fake_state() -> FakeState:
    return FakeState()


@pytest.fixture()
def client(fake_state: FakeState) -> TestClient:
    settings = Settings(
        database_url="postgresql://unused",
        admin_token="admin-secret",
        bootstrap_users=[],
        backup_dir="/tmp/prana-test-backups",
        backup_cron_hour=3,
        backup_retention_days=30,
        timezone="UTC",
    )
    app = create_app(settings=settings, enable_scheduler=False)

    @contextmanager
    def _conn_factory():
        yield FakeConnection(fake_state)

    app.state.conn_factory = _conn_factory
    return TestClient(app)
