import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

KEY_FILE = Path(os.getenv("OURA_KEY_FILE", "/data/key.hex"))
DB_FILE = Path(os.getenv("OURA_DB_FILE", "/data/oura.db"))
WATERMARK_FILE = Path(os.getenv("OURA_WATERMARK_FILE", "/data/last_event_id.txt"))
LAST_SYNC_FILE = Path(os.getenv("OURA_LAST_SYNC_FILE", "/data/last_sync_unix.txt"))
PRANA_INGEST_URL = os.getenv("PRANA_INGEST_URL", "http://localhost:9000/api/v1/ingest/oura")
OURA_INGEST_TOKEN = os.getenv("OURA_INGEST_TOKEN", "")
OURA_DEVICE_NAME = os.getenv("OURA_DEVICE_NAME", "Oura Ring 4")
OURA_SCAN_NAME = os.getenv("OURA_SCAN_NAME", "Oura")
SYNC_INTERVAL_SEC = int(os.getenv("OURA_SYNC_INTERVAL_SEC", "3600"))
SYNC_TIMEOUT_SEC = int(os.getenv("OURA_SYNC_TIMEOUT_SEC", "120"))
POLL_INTERVAL_SEC = int(os.getenv("OURA_POLL_INTERVAL_SEC", "30"))
SCAN_TIMEOUT_SEC = int(os.getenv("OURA_SCAN_TIMEOUT_SEC", "10"))
MIN_RSSI_DBM = int(os.getenv("OURA_MIN_RSSI_DBM", "-80"))
BATCH_LIMIT = 5000

_RSSI_RE = re.compile(r"(-?\d+)\s*dBm", re.IGNORECASE)


def _log(level: str, message: str, **extra: Any) -> None:
    payload: Dict[str, Any] = {"level": level, "logger": "oura_ble.sync", "message": message}
    payload.update(extra)
    print(json.dumps(payload), flush=True)


def _read_watermark() -> int:
    if not WATERMARK_FILE.exists():
        return 0
    try:
        return int(WATERMARK_FILE.read_text().strip() or "0")
    except ValueError:
        return 0


def _write_watermark(event_id: int) -> None:
    WATERMARK_FILE.write_text(str(event_id))


def _read_last_sync_ts() -> float:
    if not LAST_SYNC_FILE.exists():
        return 0.0
    try:
        return float(LAST_SYNC_FILE.read_text().strip() or "0")
    except ValueError:
        return 0.0


def _write_last_sync_ts(ts: float) -> None:
    LAST_SYNC_FILE.write_text(f"{ts:.3f}")


def parse_scan_rssi(scan_output: str) -> Optional[int]:
    """Extract the strongest RSSI (dBm) reported by `oura scan` output."""
    matches = [int(m.group(1)) for m in _RSSI_RE.finditer(scan_output or "")]
    return max(matches) if matches else None


def _is_ring_in_range() -> Tuple[bool, Optional[int]]:
    """Run a passive BLE scan and return (in_range, rssi_dbm)."""
    try:
        proc = subprocess.run(
            [
                "oura",
                "--name",
                OURA_SCAN_NAME,
                "--scan-timeout",
                str(SCAN_TIMEOUT_SEC),
                "scan",
            ],
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT_SEC + 10,
        )
    except subprocess.TimeoutExpired:
        _log("WARNING", "Oura BLE scan timed out")
        return False, None

    if proc.returncode != 0:
        return False, None
    rssi = parse_scan_rssi(proc.stdout)
    if rssi is None:
        return False, None
    return rssi >= MIN_RSSI_DBM, rssi


def _resolve_clock_anchor(conn: sqlite3.Connection) -> Tuple[Optional[float], Optional[float]]:
    """Return (anchor_ring_ds, anchor_unix_sec) using latest time_sync/rtc_beacon or row fallback."""
    row = conn.execute(
        """
        SELECT ring_timestamp, decoded_json
        FROM events
        WHERE name IN ('time_sync', 'rtc_beacon')
          AND ring_timestamp IS NOT NULL
          AND decoded_json IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        ring_ts, decoded_json = row
        try:
            body = json.loads(decoded_json)
            unix_val = body.get("unix_time") if isinstance(body, dict) else None
            if isinstance(unix_val, (int, float)) and not isinstance(unix_val, bool):
                return float(ring_ts), float(unix_val)
        except json.JSONDecodeError:
            pass

    fallback = conn.execute(
        """
        SELECT ring_timestamp, captured_unix
        FROM events
        WHERE ring_timestamp IS NOT NULL AND captured_unix IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if fallback is None:
        return None, None
    return float(fallback[0]), float(fallback[1])


def _load_new_events(
    db_path: Path,
    after_id: int,
    limit: int = BATCH_LIMIT,
) -> Tuple[List[Dict[str, Any]], int]:
    if not db_path.exists():
        return [], after_id
    events: List[Dict[str, Any]] = []
    max_id = after_id
    with sqlite3.connect(str(db_path)) as conn:
        anchor_ring, anchor_unix = _resolve_clock_anchor(conn)
        rows = conn.execute(
            """
            SELECT id, name, ring_timestamp, decoded_json, captured_unix
            FROM events
            WHERE id > ? AND decoded_json IS NOT NULL
            ORDER BY id ASC
            LIMIT ?
            """,
            (after_id, limit),
        ).fetchall()

    for row_id, name, ring_ts, decoded_json, captured_unix in rows:
        max_id = max(max_id, int(row_id))
        try:
            body = json.loads(decoded_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            body = {"value": body}
        body.setdefault("event_type", name)

        effective_ring_ts = ring_ts
        bedtime_start = body.get("bedtime_start_ds")
        if (
            name == "bedtime_period"
            and isinstance(bedtime_start, (int, float))
            and not isinstance(bedtime_start, bool)
        ):
            effective_ring_ts = float(bedtime_start)

        if effective_ring_ts is not None and anchor_ring is not None and anchor_unix is not None:
            delta_sec = (float(effective_ring_ts) - anchor_ring) / 10.0
            body["timestamp"] = round(anchor_unix + delta_sec, 3)
        else:
            body.setdefault("timestamp", captured_unix or ring_ts)
        events.append(body)

    return events, max_id


def _drain_pending_events() -> Tuple[int, Optional[Dict[str, Any]]]:
    """Send all pending decoded Oura events in batches and advance watermark per batch."""
    if not DB_FILE.exists():
        return 0, None

    total_ingested = 0
    last_result: Optional[Dict[str, Any]] = None
    while True:
        last_id = _read_watermark()
        events, new_max_id = _load_new_events(DB_FILE, last_id, limit=BATCH_LIMIT)
        if new_max_id <= last_id:
            break
        if events:
            resp = httpx.post(
                PRANA_INGEST_URL,
                headers={"X-Ingest-Token": OURA_INGEST_TOKEN},
                json={"device": OURA_DEVICE_NAME, "events": events},
                timeout=30.0,
            )
            resp.raise_for_status()
            last_result = resp.json()
            total_ingested += len(events)
        _write_watermark(new_max_id)
    return total_ingested, last_result


def sync_once(now: Optional[float] = None) -> str:
    """Run one presence- and cooldown-gated sync cycle.

    Only updates ``LAST_SYNC_FILE`` when both ``oura sync`` and all Prana
    ingest batches finish without error.
    """
    if not KEY_FILE.exists():
        _log(
            "WARNING",
            f"Key file {KEY_FILE} not found. Pair ring first with: "
            f"docker compose --profile oura run --rm oura-ble oura --key-file {KEY_FILE} pair",
        )
        return "unconfigured"

    if not OURA_INGEST_TOKEN:
        _log("ERROR", "OURA_INGEST_TOKEN is not set; skipping sync")
        return "unconfigured"

    drained_backlog, backlog_result = _drain_pending_events()
    if drained_backlog > 0:
        _log(
            "INFO",
            "Drained pending Oura BLE events to Prana",
            events_ingested=drained_backlog,
            result=backlog_result,
        )

    now_ts = time.time() if now is None else float(now)
    last_sync_ts = _read_last_sync_ts()
    if last_sync_ts > 0.0 and (now_ts - last_sync_ts) < SYNC_INTERVAL_SEC:
        return "cooldown"

    in_range, rssi = _is_ring_in_range()
    if not in_range:
        if rssi is not None:
            _log("INFO", f"Oura Ring RSSI {rssi} dBm below threshold ({MIN_RSSI_DBM} dBm); skipping sync")
        return "out_of_range"

    _log("INFO", f"Oura Ring detected in range (RSSI {rssi} dBm); starting sync")
    subprocess.run(
        ["oura", "--key-file", str(KEY_FILE), "--db", str(DB_FILE), "sync"],
        check=True,
        timeout=SYNC_TIMEOUT_SEC,
    )

    total_ingested, last_result = _drain_pending_events()
    completed_ts = time.time() if now is None else float(now)
    _write_last_sync_ts(completed_ts)

    if total_ingested == 0:
        _log("INFO", "Oura BLE sync completed; no new events to ingest")
    else:
        _log(
            "INFO",
            "Synced raw Oura BLE events to Prana",
            events_ingested=total_ingested,
            result=last_result,
        )
    return "synced"


def main() -> None:
    _log(
        "INFO",
        "Starting presence-triggered Oura BLE sync loop",
        sync_interval_sec=SYNC_INTERVAL_SEC,
        poll_interval_sec=POLL_INTERVAL_SEC,
        min_rssi_dbm=MIN_RSSI_DBM,
    )
    last_state: Optional[str] = None
    while True:
        try:
            state = sync_once()
            if state != last_state and state in ("cooldown", "out_of_range"):
                _log("INFO", f"Oura sync watcher state: {state}")
            last_state = state
        except Exception as exc:
            last_state = "error"
            _log("ERROR", f"Oura sync cycle failed: {exc}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
