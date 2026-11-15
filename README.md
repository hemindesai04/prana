# Prana

Self-hosted, multi-user health biomarker hub for **Raspberry Pi 4**. Ingests Apple Watch (via Health Auto Export) and Oura Ring 4 (via local BLE, no subscription required) into PostgreSQL, with automated backups, twice-daily AI/evidence briefings, and Grafana cross-wearable dashboards.

> See [ARCHITECTURE.md](ARCHITECTURE.md), [IMPLEMENTATION.md](IMPLEMENTATION.md), and [AGENTS.md](AGENTS.md) for system design, roadmap, and agent rules.

---

## Quick Start (Raspberry Pi 4 / Localhost)

### 1. Clone & Configure
```bash
git clone https://github.com/hemindesai04/prana.git
cd prana
cp .env.example .env
```

Edit `.env` with strong secrets:
- `POSTGRES_PASSWORD`: Strong database password
- `ADMIN_TOKEN`: Token for creating additional household users via `POST /api/v1/users` and reprocessing via `POST /api/v1/admin/reprocess`
- `BOOTSTRAP_USERS`: Comma-separated `slug:token` pairs created automatically on startup (e.g. `hemin:super_secret_token_1,alex:super_secret_token_2`)
- `OURA_INGEST_TOKEN`: User ingest token (from `BOOTSTRAP_USERS`) used by the `oura-ble` service when posting to `POST /api/v1/ingest/oura`

### 2. Start the 2-Container Stack
```bash
docker compose up -d --build
```

Verify health:
```bash
curl http://localhost:9000/health
```

### 3. Connect Apple Watch 8 (Health Auto Export iOS App)
1. Find your Pi's local IP (`hostname -I`).
2. In **Health Auto Export** → **Automations** → **Create Automation** → **REST API**:
   - **URL:** `http://<PI_LOCAL_IP>:9000/api/v1/ingest/apple-health`
   - **Header:** `X-Ingest-Token: <your user token from BOOTSTRAP_USERS>`
   - **Format:** `JSON`
   - **Metrics:** Heart Rate, HRV, Sleep Analysis, VO2 Max, Respiratory Rate, Resting Heart Rate, Steps.
3. Trigger a manual sync and verify data landed:
```bash
curl -H "X-Ingest-Token: <your user token>" http://localhost:9000/api/v1/stats
```

### 4. Connect Oura Ring 4 (Local BLE, No Subscription)
1. Place the Oura Ring 4 on its charger (blinking blue pairing mode) near the Pi and pair it once:
```bash
docker compose --profile oura run --rm oura-ble oura --key-file /data/key.hex pair
```
2. Start the `oura-ble` presence-triggered sync service (1-hour cooldown after each successful `POST /api/v1/ingest/oura` sync):
```bash
docker compose --profile oura up -d --build oura-ble
```
3. Verify Oura ingestion:
```bash
curl -H "X-Ingest-Token: <your user token>" "http://localhost:9000/api/v1/stats?source=oura_ble"
```

---

## Running Tests
Run the full unit + PostgreSQL 18.7 integration suite inside Docker (zero host installs):
```bash
docker compose --profile test run --rm tests
docker compose --profile test rm -s -f db-test
```

---

## Ports
- `9000` — Prana Ingestor & Analyzer API (`http://<PI_IP>:9000`)
- `9300` — Grafana Visualization Dashboard (`http://localhost:9300`, via `--profile dashboard`)

---

## Visualization Dashboard (Grafana)
Start the provisioned Grafana service alongside Prana using the read-only `prana_ro` role:
```bash
docker compose --profile dashboard up -d --build
```
Open `http://localhost:9300` (default credentials in `.env.example`: `admin` / `prana_admin`, change via `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`) and view **Prana — Daily Health, Sleep & Recovery** (including separate Oura Ring vs Apple Watch series and daily `Oura − Apple Watch` delta charts).

---

## Daily Analysis & Briefings (08:00 & 22:00)
Prana rolls up `daily_metrics`, evaluates physiological rules A–H into `insights`, and generates morning (`08:00`) and evening (`22:00`) briefings automatically (or manually anytime):
```bash
curl -X POST http://localhost:9000/api/v1/analysis/run \
  -H "X-Ingest-Token: <your user token>" \
  -H "Content-Type: application/json" \
  -d '{"days_back": 60, "period": "both"}'

curl -H "X-Ingest-Token: <your user token>" http://localhost:9000/api/v1/insights
```
To enable LLM-synthesized briefings via Google Gemini, add a Google AI Studio key to `.env` (`GEMINI_API_KEY=...`, default model `gemini-2.5-flash`). Without a key, Prana automatically generates deterministic evidence-backed briefings.

---

## Backups & Restore
Nightly `pg_dump -Fc` backups run automatically inside the `prana` container at `03:00` (configurable via `BACKUP_CRON_HOUR`) and write `0600` `.dump` files to `./backups`.

To restore a backup into the running `db` container:
```bash
docker compose exec -T prana python -m prana.maintenance.backup restore /backups/<file>.dump
```

## Status & Monitoring
- `GET /health` — Lightweight liveness check (`200` if DB reachable, `503` otherwise).
- `GET /api/v1/status` — Overall health (`ok | degraded | critical`) across DB, migrations, ingestion SOS/warnings, failed raw rows, backups, and scheduler. Pass `?detail=1` with `X-Admin-Token` for open issue details.
