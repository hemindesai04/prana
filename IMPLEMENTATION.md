# IMPLEMENTATION.md — Phased Roadmap

## Phase 0: Repository & Guardrails (Complete)
- Clean repository initialized with `ARCHITECTURE.md`, `IMPLEMENTATION.md`, `AGENTS.md`, and `README.md`.

## Phase 1A: Containerized Core, Multi-User Apple Watch Ingestion & Backups (Complete)
- **Deliverables:**
  - `docker-compose.yml` (`db` + `prana` on port `9000`, JSON log rotation).
  - Multi-user PostgreSQL schema (`app/prana/models.py` + `app/alembic/`) with token-based user bootstrap (`BOOTSTRAP_USERS`).
  - FastAPI endpoints: `GET /health`, `POST /api/v1/ingest/apple-health`, `POST /api/v1/ingest/points`, `GET /api/v1/stats`, `GET /api/v1/status`, `POST /api/v1/users`, `POST /api/v1/admin/reprocess`.
  - Containerized nightly `pg_dump` backup job via APScheduler.
  - Unit tests (`tests/test_unit_parsers.py`) and flow tests (`tests/test_flow_ingest.py`).
- **Acceptance Criteria:**
  - `pytest -v` passes 100%.
  - `docker compose up -d --build` succeeds on Raspberry Pi 4.
  - Health Auto Export pushes JSON from iPhone to `http://<PI_IP>:9000/api/v1/ingest/apple-health` and records appear in `GET /api/v1/stats`.

## Phase 1B: Oura Ring 4 Local BLE Ingestion (Complete)
- **Deliverables:**
  - Add `--profile oura` container (`Th0rgal/open_oura`) with D-Bus mount (`/var/run/dbus:/var/run/dbus`), syncing hourly.
  - Dedicated `POST /api/v1/ingest/oura` endpoint & parser (`app/prana/ingestor/oura.py`) with raw-first storage, canonical mapping, drift warnings, SOS issue tracking, and `/api/v1/admin/reprocess` support (`source="oura_ble"`).
  - Unit + flow tests for Oura event normalization and contract validation.
- **Acceptance Criteria:**
  - Oura Ring 4 syncs automatically when near the Pi 4 and appears alongside `apple_health` in `/api/v1/stats`.

## Phase 2: Custom Domain & HTTPS Edge
- **Deliverables:**
  - Add `cloudflared` (`--profile tunnel`) to `docker-compose.yml`.
  - Route `https://ingest.<domain>` → `http://prana:9000`.
- **Acceptance Criteria:**
  - Health Auto Export syncs over cellular/remote Wi-Fi via HTTPS.

## Phase 3: AI Analysis & Proactive Notifications
- **Deliverables:**
  - `app/prana/analyzer/` (rolling baselines, z-score anomaly detection, cross-source comparison, LLM synthesis).
  - `app/prana/notifier/` (Apprise integration per `user_id`).
  - Unit + flow tests for anomaly detection and notification formatting.

## Phase 4: Visualization Dashboard
- **Deliverables:**
  - `grafana` (`--profile dashboard`, port `9300`) with provisioned read-only Postgres datasource and multi-user biomarker dashboards.
