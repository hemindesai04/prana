import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any, Dict, List

import httpx

KEY_FILE = Path(os.getenv("OURA_KEY_FILE", "/data/key.hex"))
DB_FILE = Path(os.getenv("OURA_DB_FILE", "/data/oura.db"))
WATERMARK_FILE = Path(os.getenv("OURA_WATERMARK_FILE", "/data/last_event_id.txt"))
PRANA_INGEST_URL = os.getenv("PRANA_INGEST_URL", "http://localhost:9000/api/v1/ingest/oura")
OURA_INGEST_TOKEN = os.getenv("OURA_INGEST_TOKEN", "")
OURA_DEVICE_NAME = os.getenv("OURA_DEVICE_NAME", "Oura Ring 4")
SYNC_INTERVAL_SEC = int(os.getenv("OURA_SYNC_INTERVAL_SEC", "3600"))
SYNC_TIMEOUT_SEC = int(os.getenv("OURA_SYNC_TIMEOUT_SEC", "120"))


def _read_watermark() -> int:
    if not WATERMARK_FILE.exists():
        return 0
    try:
        return int(WATERMARK_FILE.read_text().strip() or "0")
    except ValueError:
        return 0


def _write_watermark(event_id: int) -> None:
    WATERMARK_FILE.write_text(str(event_id))


def _load_new_events(db_path: Path, after_id: int) -> tuple[List[Dict[str, Any]], int]:
    if not db_path.exists():
        return [], after_id
    events: List[Dict[str, Any]] = []
    max_id = after_id
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT id, name, ring_timestamp, decoded_json, captured_unix
            FROM events
            WHERE id > ? AND decoded_json IS NOT NULL
            ORDER BY id ASC
            LIMIT 5000
            """,
            (after_id,),
        ).fetchall()
    parsed_rows: List[tuple[int, str, Any, Dict[str, Any], Any]] = []
    for row_id, name, ring_ts, decoded_json, captured_unix in rows:
        max_id = max(max_id, int(row_id))
        try:
            body = json.loads(decoded_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            body = {"value": body}
        body.setdefault("event_type", name)
        parsed_rows.append((int(row_id), name, ring_ts, body, captured_unix))

    ring_vals = [float(r) for _, _, r, _, _ in parsed_rows if r is not None]
    cap_vals = [float(c) for _, _, _, _, c in parsed_rows if c is not None]
    anchor_ring = max(ring_vals) if ring_vals else None
    anchor_unix = max(cap_vals) if cap_vals else None

    for _, _, ring_ts, body, captured_unix in parsed_rows:
        if ring_ts is not None and anchor_ring is not None and anchor_unix is not None:
            delta_sec = max(0.0, (anchor_ring - float(ring_ts)) / 10.0)
            body["timestamp"] = round(anchor_unix - delta_sec, 3)
        else:
            body.setdefault("timestamp", captured_unix or ring_ts)
        events.append(body)
    return events, max_id


def sync_once() -> None:
    if not KEY_FILE.exists():
        print(
            json.dumps(
                {
                    "level": "WARNING",
                    "logger": "oura_ble.sync",
                    "message": f"Key file {KEY_FILE} not found. Pair ring first with: "
                    f"docker compose --profile oura run --rm oura-ble oura --key-file {KEY_FILE} pair",
                }
            ),
            flush=True,
        )
        return

    if not OURA_INGEST_TOKEN:
        print(
            json.dumps(
                {
                    "level": "ERROR",
                    "logger": "oura_ble.sync",
                    "message": "OURA_INGEST_TOKEN is not set; skipping sync",
                }
            ),
            flush=True,
        )
        return

    subprocess.run(
        ["oura", "--key-file", str(KEY_FILE), "--db", str(DB_FILE), "sync"],
        check=True,
        timeout=SYNC_TIMEOUT_SEC,
    )

    last_id = _read_watermark()
    events, new_max_id = _load_new_events(DB_FILE, last_id)
    if not events:
        print(
            json.dumps(
                {
                    "level": "INFO",
                    "logger": "oura_ble.sync",
                    "message": "No new Oura events to ingest",
                }
            ),
            flush=True,
        )
        return

    resp = httpx.post(
        PRANA_INGEST_URL,
        headers={"X-Ingest-Token": OURA_INGEST_TOKEN},
        json={"device": OURA_DEVICE_NAME, "events": events},
        timeout=30.0,
    )
    resp.raise_for_status()
    _write_watermark(new_max_id)
    print(
        json.dumps(
            {
                "level": "INFO",
                "logger": "oura_ble.sync",
                "message": "Synced raw Oura BLE events to Prana",
                "result": resp.json(),
            }
        ),
        flush=True,
    )


def main() -> None:
    while True:
        try:
            sync_once()
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "level": "ERROR",
                        "logger": "oura_ble.sync",
                        "message": f"Oura sync cycle failed: {exc}",
                    }
                ),
                flush=True,
            )
        time.sleep(SYNC_INTERVAL_SEC)


if __name__ == "__main__":
    main()
