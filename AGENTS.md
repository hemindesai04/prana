# AGENTS.md — Prana Repository Guide for Coding Agents

Self-hosted, multi-user, containerized health biomarker hub targeting **Raspberry Pi 4 (ARM64)** via **Docker Compose**.
For system topology and SQL/HTTP contracts, read [ARCHITECTURE.md](ARCHITECTURE.md). For phase scope, read [IMPLEMENTATION.md](IMPLEMENTATION.md).

---

## 1. Environment & Essential Commands

- **Local Python / Containerized Tests:** Never install into global Python or onto the Pi host OS.
  - Run full unit + Postgres 18.7 integration suite: `docker compose --profile test run --rm tests && docker compose --profile test rm -s -f db-test`
  - Optional local `.venv/` (if present): `PYTHONPATH=app .venv/bin/pytest -v tests`
  - Add pinned runtime deps to `app/requirements.txt`; dev/test deps to `app/requirements-dev.txt`.
- **Containerized Runtime (Raspberry Pi 4 / Docker):**
  - Validate compose: `docker compose config -q`
  - Start / rebuild: `docker compose up -d --build`
  - Healthcheck: `curl -fsS http://localhost:9000/health`

---

## 2. Repository Map & Module Boundaries

- `docker-compose.yml` — `db` (`postgres:18.7-alpine`) + `prana` (`:9000`, non-root UID 1000) + `test` profile (`db-test` + `tests`) with `json-file` log rotation (`10m` × 5).
- `app/prana/models.py` & `app/alembic/` — SQLAlchemy 2.0 models and Alembic migrations (`users`, `raw_ingestions`, `biomarkers`, `workouts`, `ingestion_issues`, `backup_runs`), auto-upgraded on startup.
- `db/init/01_roles.sql` — Creates the read-only `prana_ro` role for smoke tests and future dashboards.
- `app/prana/ingestor/` — HTTP routes, canonical metric registry, raw-first storage, & payload parsers (connectors talk to `prana` **only** via `/api/v1/ingest/*`).
- `oura-ble/` — Containerized Oura Ring 4 BLE sync service (`--profile oura`, `Th0rgal/open_oura` + hourly `sync.py` posting to `/api/v1/ingest/oura`).
- `app/prana/maintenance/` — Containerized scheduled `pg_dump -Fc` backups (`/backups`) and `pg_restore` helper.
- `app/prana/logging_setup.py` — Structured JSON logger (`get_logger(__name__)`) to `stdout`.
- `tests/` — Unit (`test_unit_*.py`), Postgres 18.7 integration (`test_flow_*.py`), and optional `prana_ro` read-only prod smoke tests (`test_prod_ro_smoke.py`).

---

## 3. Critical Conventions & Gotchas

- **Multi-user isolation:** Every ingestion, query, and future analysis/notification must scope by `user_id` resolved from `X-Ingest-Token` (stored as SHA-256 in `users.ingest_token_hash`).
- **Raw data preservation:** Always commit raw payloads into `raw_ingestions` (`JSONB`, deduplicated by `payload_sha256`) **before** normalization, and record parse/contract issues in `ingestion_issues` (`sos` / `warning`).
- **Idempotency:** Upserts rely on `UNIQUE (user_id, source, device, metric_name, recorded_at)` and `UNIQUE (user_id, source, device, workout_type, started_at)`. Never remove these constraints.
- **Port series `9xxx`:** `9000` = API, `9300` = future Grafana. Port `8xxx` is reserved (e.g., Home Assistant).
- **Schema changes:** Always add an additive Alembic migration in `app/alembic/versions/` matching `app/prana/models.py`. Never edit applied migrations destructively.
- **Single-worker requirement:** APScheduler runs in-process; never launch uvicorn with `--workers > 1`.
- **Documentation sync:** For every change, review and update all affected docs in the same change (`ARCHITECTURE.md` for technical architecture, the HTTP API Endpoints table, ports, services, and schema—never add phase or implementation roadmap details to `ARCHITECTURE.md`; `README.md` for setup/usage/endpoints; `IMPLEMENTATION.md` for phase status; `AGENTS.md` for conventions). Static checks in the `tests` container enforce `ruff` linting and exact parity between FastAPI routes and `ARCHITECTURE.md`'s HTTP API Endpoints table.

---

## 4. Strict Boundaries (Do Not Do)

- **Never delete data or volumes:** Never run `docker compose down -v`, `rm -rf pgdata`, `DROP TABLE`, or delete `backups/`.
- **Never run bare-metal host installs:** No host `pip install`, `apt install`, or host `crontab` for app logic—everything runs inside Docker.
- **Never commit secrets or health data:** `.env`, `.venv/`, `backups/`, `pgdata/`, `*.log`, and health exports must stay gitignored.
- **Scope control:** Touch only files required for the requested task; do not refactor unrelated modules.

---

## 5. Mandatory Verification Protocol

Before finishing any change:
1. Write or update **both unit tests and flow tests** covering the scope of your change.
2. Run `docker compose --profile test run --rm tests && docker compose --profile test rm -s -f db-test` (or `PYTHONPATH=app .venv/bin/pytest -v tests` if running locally) and observe **all tests passing**.
3. If `docker-compose.yml` or `Dockerfile` changed, run `docker compose config -q` (on a Docker-enabled host).
4. Review and update `ARCHITECTURE.md` (technical details only, no phases), `README.md`, `IMPLEMENTATION.md`, and `AGENTS.md` as needed so documentation matches the final code.
