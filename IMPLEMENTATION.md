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
  - Add `--profile oura` container (`Th0rgal/open_oura`) with D-Bus mount (`/var/run/dbus:/var/run/dbus`), syncing on BLE presence with a 1-hour successful-sync cooldown.
  - Dedicated `POST /api/v1/ingest/oura` endpoint & parser (`app/prana/ingestor/oura.py`) with raw-first storage, canonical mapping, drift warnings, SOS issue tracking, and `/api/v1/admin/reprocess` support (`source="oura_ble"`).
  - Unit + flow tests for Oura event normalization and contract validation.
- **Acceptance Criteria:**
  - Oura Ring 4 syncs automatically when near the Pi 4 and appears alongside `apple_health` in `/api/v1/stats`.

## Phase 2: Custom Domain & HTTPS Edge (Deferred — Localhost Hosting Active)
- **Deliverables:**
  - `cloudflared` (`--profile tunnel`) in `docker-compose.yml` when a custom domain is attached.
  - Route `https://ingest.<domain>` → `http://prana:9000`.

## Phase 3: Daily Analytics, Evidence Rules & Twice-Daily Briefings (Core Complete; Push Channel Deferred)
- **Deliverables:**
  - `app/prana/analyzer/` (`rollup.py`, `rules.py`, `briefing.py`): sleep interval deduplication & device separation, missing-day exclusion, 60-day rolling baselines & z-scores, 14-day sleep debt, ACWR, Rules A–H, and scheduled morning (`08:00`) / evening (`22:00`) + on-demand briefings via Gemini API with deterministic fallback.
  - `POST /api/v1/analysis/run` and `GET /api/v1/insights` API endpoints.
  - Unit (`tests/test_unit_analyzer.py`) and Postgres 18.7 integration tests (`tests/test_flow_ingest.py`).
  - *Remaining:* External push notification delivery channel (`app/prana/notifier/`) once desired channel is chosen.

## Phase 4: Visualization Dashboard (Complete)
- **Deliverables:**
  - `grafana` (`--profile dashboard`, `grafana/grafana-oss:11.5.2`, port `9300`) with provisioned read-only Postgres datasource (`prana_ro`) and multi-user daily health dashboard (`grafana/dashboards/prana_daily_health.json`) displaying briefings, sleep debt, VO2max, separate Apple Watch vs Oura sleep curves, autonomic baselines, ACWR, and insights.
