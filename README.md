# Prana

Self-hosted, multi-user health biomarker hub for **Raspberry Pi 4**. Ingests Apple Watch (via Health Auto Export) and Oura Ring 4 (via local BLE, no subscription required) into PostgreSQL 18.7, with automated `pg_dump` backups, physiological trend & correlation rules (A–H), twice-daily Gemini AI/evidence briefings, and a Grafana cross-wearable dashboard.

> See [ARCHITECTURE.md](ARCHITECTURE.md), [IMPLEMENTATION.md](IMPLEMENTATION.md), and [AGENTS.md](AGENTS.md) for system design, roadmap status, and agent rules.

---

## Container Stack

| Service | Container | Profile | Port | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `db` | `prana-db` (`postgres:18.7-alpine`) | default | `5432` (internal) | Primary multi-user PostgreSQL 18 database (`pgdata` volume) + read-only `prana_ro` role ([01_roles.sql](db/init/01_roles.sql)) |
| `prana` | `prana-app` (`./app`, UID `1000`) | default | `9000` | FastAPI ingestor & analyzer (`uvicorn` single worker), auto-Alembic migrations, APScheduler (`03:00` backups, `08:00` & `22:00` briefings) |
| `oura-ble` | `prana-oura-ble` (`./oura-ble`, `network_mode: host`) | `oura` | — | Local Oura Ring 4 BLE presence watcher (`Th0rgal/open_oura` + `sync.py`) via host D-Bus (`/var/run/dbus`), `oura_data` volume |
| `grafana` | `prana-grafana` (`grafana/grafana-oss:11.5.2`) | `dashboard` | `9300` | Provisioned Grafana dashboard querying PostgreSQL via `prana_ro` (`grafana_data` volume) |
| `db-test` + `tests` | Ephemeral (`tmpfs` DB + test runner) | `test` | — | Isolated PostgreSQL 18.7 integration + unit test suite + `ruff` linter |

---

## Quick Start (Raspberry Pi 4 / Localhost)

### 1. Clone & Configure
```bash
git clone https://github.com/hemindesai04/prana.git
cd prana
cp .env.example .env
```

Edit `.env` before starting:

- **Core DB & Auth:**
  - `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`: PostgreSQL credentials (defaults `prana` / strong secret / `prana`)
  - `ADMIN_TOKEN`: Admin header (`X-Admin-Token`) for `POST /api/v1/users`, `POST /api/v1/admin/reprocess`, and `GET /api/v1/status?detail=1`
  - `BOOTSTRAP_USERS`: Comma-separated `slug:token` pairs seeded on startup (e.g. `hemin:super_secret_token_1,alex:super_secret_token_2`)
- **Oura Ring 4 BLE (`--profile oura`):**
  - `OURA_INGEST_TOKEN`: User ingest token (from `BOOTSTRAP_USERS`) used when posting to `POST /api/v1/ingest/oura`
  - `OURA_DEVICE_NAME`: Device label stored in `biomarkers` / `daily_metrics` (default `Oura Ring 4`)
  - `OURA_SYNC_INTERVAL_SEC`: Minimum cooldown in seconds after a successful sync (default `3600`)
- **AI Briefings & Schedules:**
  - `GEMINI_API_KEY`: Google AI Studio key for Gemini briefings (if unset, Prana falls back cleanly to deterministic evidence templates)
  - `GEMINI_MODEL`: Gemini REST model ID (default `gemini-2.5-flash` in `.env.example`, or e.g. `gemini-3.5-flash-lite`)
  - `MORNING_BRIEFING_HOUR` / `EVENING_BRIEFING_HOUR`: Daily briefing schedule hours in server `TZ` (defaults `8` and `22`)
- **Backups & Timezone:**
  - `TZ`: Server schedule timezone (e.g. `America/Los_Angeles`; default `UTC`)
  - `BACKUP_CRON_HOUR` / `BACKUP_RETENTION_DAYS`: Nightly `pg_dump -Fc` hour and retention window (defaults `3` and `30`)
- **Grafana (`--profile dashboard`):**
  - `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`: Grafana login (defaults `admin` / `prana_admin`)
  - `POSTGRES_RO_PASSWORD`: Read-only Postgres password for `prana_ro` (default `prana_ro`)

### 2. Start the Stack

Start the **full 4-container stack** (`db` + `prana` + `oura-ble` + `grafana`):
```bash
docker compose --profile oura --profile dashboard up -d --build
```

*(Or start only the core 2-container API + database stack: `docker compose up -d --build`.)*

Verify service health and pipeline readiness:
```bash
curl -fsS http://localhost:9000/health
curl -fsS http://localhost:9000/api/v1/status
```

### 3. Connect Apple Watch (Health Auto Export iOS App)
1. Find your Pi's local IP (`hostname -I`).
2. In **Health Auto Export** → **Automations** → **Create Automation** → **REST API**:
   - **URL:** `http://<PI_LOCAL_IP>:9000/api/v1/ingest/apple-health`
   - **Header:** `X-Ingest-Token: <your user token from BOOTSTRAP_USERS>`
   - **Format:** `JSON`
   - **Metrics:** `heart_rate`, `resting_heart_rate`, `walking_heart_rate_average`, `heart_rate_variability`, `respiratory_rate`, `vo2_max`, `blood_oxygen_saturation`, `step_count`, `sleep_analysis`, `active_energy`, `basal_energy_burned`, `apple_sleeping_wrist_temperature`, `apple_exercise_time`, `apple_stand_time`, `weight_body_mass`, plus workouts.
3. Trigger a manual sync and verify data landed:
```bash
curl -H "X-Ingest-Token: <your user token>" http://localhost:9000/api/v1/stats
```

### 4. Connect Oura Ring 4 (Local BLE, No Subscription)
1. Place the Oura Ring 4 on its charger (blinking blue pairing mode) near the Pi and pair it once:
```bash
docker compose --profile oura run --rm oura-ble oura --key-file /data/key.hex pair
```
2. Start or verify the `oura-ble` watcher (if not already started with `--profile oura`):
```bash
docker compose --profile oura up -d --build oura-ble
```
   - `oura-ble/sync.py` polls BLE every `OURA_POLL_INTERVAL_SEC` (`30s`) with `OURA_SCAN_TIMEOUT_SEC` (`10s`), triggers `oura sync` when RSSI ≥ `OURA_MIN_RSSI_DBM` (`-80 dBm`) and elapsed time since `/data/last_sync_unix.txt` ≥ `OURA_SYNC_INTERVAL_SEC` (`3600s`), drains unsynced SQLite events in `5,000`-event batches to `POST /api/v1/ingest/oura`, and updates the cooldown timestamp **only** after both BLE sync and API ingestion succeed.
3. Verify Oura ingestion:
```bash
curl -H "X-Ingest-Token: <your user token>" "http://localhost:9000/api/v1/stats?source=oura_ble"
```

---

## Visualization Dashboard (Grafana `:9300`)

Open `http://<PI_LOCAL_IP>:9300` (or `http://localhost:9300` on the Pi; default credentials `admin` / `prana_admin`) and select **Prana — Daily Health, Sleep & Recovery**.

The dashboard reads PostgreSQL exclusively through the `prana_ro` role and is organized into 5 rows:
1. **Overview & Twice-Daily Briefings:** 14-day sleep debt, latest VO2max, HRV, Resting HR, plus Morning & Evening briefings displaying local `generated_at` timestamps (`YYYY-MM-DD HH24:MI`).
2. **Sleep Duration & 14-Day Sleep Debt:** Separate Apple Watch vs Oura Ring sleep curves (unrecorded nights excluded) and rolling cumulative sleep debt.
3. **Autonomic Recovery & Early Illness / Strain Signals:** HRV (`SDNN` vs `RMSSD`) + 60-day baselines, separate Apple Watch vs Oura Resting Heart Rate, Respiratory Rate, and separate Apple Watch vs Oura Skin/Wrist Temperature deltas.
4. **Cross-Wearable Comparison (`Oura Ring vs Apple Watch`):** Daily difference chart (`Oura − Apple Watch` on overlapping days) for Sleep Duration (`hr`), Mean HR (`bpm`), Resting HR (`bpm`), Skin Temp (`°C`), and HRV (`Oura RMSSD − Watch SDNN`, `ms`), paired with side-by-side Mean HR & Skin Temp curves and a 30-day concordance table.
5. **Fitness, Training Load, Activity & Insights:** VO2max trend, ACWR (`0.8–1.3`), daily steps, and Rules A–H findings with local `generated_at` timestamps.

---

## Autopilot Analysis & Twice-Daily Briefings

Prana runs a hybrid event-driven + scheduled analytics pipeline:
- **Real-Time Post-Sync Rollups:** Every wearable sync (`/api/v1/ingest/*`) automatically recomputes the 14-day `daily_metrics` rollup (strictly skipping missing days and keeping Apple Watch vs Oura separate) and re-evaluates evidence-based Rules A–H (`insights`) **without** invoking Gemini on every sync.
- **Morning (`08:00`) & Evening (`22:00`) AI Briefings:** APScheduler runs twice daily in configured `TZ` (while date attribution uses each user's `users.timezone`):
  - If morning sleep has not synced yet at `08:00`, Prana saves a placeholder and automatically invokes Gemini **once** when that day's sleep first arrives.
  - Evening briefings run at `22:00`, with automatic recovery on subsequent syncs after `22:00` if the slot fell back to deterministic.
  - Gemini calls retry transient `408`/`429`/`5xx` and network/timeout errors with backoff before falling back to a deterministic template, and a deterministic fallback never overwrites an already-generated Gemini briefing for the same `(user_id, day, period)`.

Run analysis manually or inspect current profile metrics, insights, and briefings anytime:
```bash
curl -X POST http://localhost:9000/api/v1/analysis/run \
  -H "X-Ingest-Token: <your user token>" \
  -H "Content-Type: application/json" \
  -d '{"days_back": 60, "period": "both"}'

curl -H "X-Ingest-Token: <your user token>" http://localhost:9000/api/v1/insights
```

---

## HTTP API Reference & Administration

| Method | Path | Auth | Purpose |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | None | Liveness probe (`200` if DB reachable, `503` otherwise) |
| `GET` | `/api/v1/status` | None (`X-Admin-Token` for `?detail=1`) | Aggregate status across DB, migrations, SOS/warnings, failed rows, backups, and scheduler |
| `GET` | `/api/v1/stats` | `X-Ingest-Token` | Per-user biomarker, workout, and raw ingestion counts (`?source=` optional) |
| `POST` | `/api/v1/users` | `X-Admin-Token` | Create or update a household user (`slug`, `timezone`, DOB, sex, sleep target, token) |
| `POST` | `/api/v1/ingest/apple-health` | `X-Ingest-Token` | Ingest Health Auto Export JSON payloads |
| `POST` | `/api/v1/ingest/oura` | `X-Ingest-Token` | Ingest Oura Ring BLE event batches |
| `POST` | `/api/v1/ingest/points` | `X-Ingest-Token` | Generic normalized point ingestion for custom connectors |
| `POST` | `/api/v1/admin/reprocess` | `X-Admin-Token` | Re-run normalization on stored `raw_ingestions` |
| `POST` | `/api/v1/analysis/run` | `X-Ingest-Token` | Run daily rollups, Rules A–H evaluation, and morning/evening briefings |
| `GET` | `/api/v1/insights` | `X-Ingest-Token` | Fetch user profile metrics, active insights, and recent briefings |

---

## Backups & Restore

Nightly `pg_dump -Fc` backups run automatically inside `prana-app` at `03:00` (configurable via `BACKUP_CRON_HOUR`) and write `0600` `.dump` files to `./backups`, pruning files older than `BACKUP_RETENTION_DAYS`.

To restore a backup into the running `db` container:
```bash
docker compose exec -T prana python -m prana.maintenance.backup restore /backups/<file>.dump
```

---

## Running Tests & Linting

Validate Compose configuration and run the full `ruff` lint check, route-doc parity check, unit suite, and PostgreSQL 18.7 integration suite inside Docker (zero host installs):
```bash
docker compose config -q
docker compose --profile test run --rm tests
docker compose --profile test rm -s -f db-test
```
