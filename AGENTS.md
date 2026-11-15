# AGENTS.md — Prana Repository Guide for Coding Agents

Self-hosted, multi-user, containerized health biomarker hub targeting **Raspberry Pi 4 (ARM64)** via **Docker Compose**.
For system topology and SQL/HTTP contracts, read [ARCHITECTURE.md](ARCHITECTURE.md). For phase scope, read [IMPLEMENTATION.md](IMPLEMENTATION.md).

---

## 1. Environment & Essential Commands

- **Local Python (dev/tests only):** Use the project-local `.venv/` (never install into global Python or onto the Pi host OS).
  - Run tests: `PYTHONPATH=app .venv/bin/pytest -v tests`
  - Add runtime deps to `app/requirements.txt`; dev/test deps to `app/requirements-dev.txt`.
- **Containerized Runtime (Raspberry Pi 4 / Docker):**
  - Validate compose: `docker compose config -q`
  - Start / rebuild: `docker compose up -d --build`
  - Healthcheck: `curl -fsS http://localhost:9000/health`

---

## 2. Repository Map & Module Boundaries

- `docker-compose.yml` — `db` (`postgres:16-alpine`) + `prana` (`:9000`) with `json-file` log rotation (`10m` × 5).
- `db/init/01_schema.sql` — Multi-user schema (`users`, `raw_ingestions`, `biomarkers`, `workouts`).
- `app/prana/ingestor/` — HTTP routes & payload parsers (connectors talk to `prana` **only** via `/api/v1/ingest/*`).
- `app/prana/maintenance/` — Containerized scheduled `pg_dump` backups (`/backups`).
- `app/prana/logging_setup.py` — Structured JSON logger (`get_logger(__name__)`) to `stdout`.
- `tests/` — Unit (`test_unit_*.py`) and flow (`test_flow_*.py`) tests using `FakeConnection` / `TestClient`.

---

## 3. Critical Conventions & Gotchas

- **Multi-user isolation:** Every ingestion, query, and future analysis/notification must scope by `user_id` resolved from `X-Ingest-Token` (stored as SHA-256 in `users.ingest_token_hash`).
- **Raw data preservation:** Always insert raw payloads into `raw_ingestions` (`JSONB`) alongside normalized rows.
- **Idempotency:** Upserts rely on `UNIQUE (user_id, source, metric_name, recorded_at)` and `UNIQUE (user_id, source, workout_type, started_at)`. Never remove these constraints.
- **Port series `9xxx`:** `9000` = API, `9300` = future Grafana. Port `8xxx` is reserved (e.g., Home Assistant).
- **Schema changes:** Never edit `db/init/01_schema.sql` destructively for an already-initialized DB; add additive migrations instead.
- **Documentation sync:** Update `ARCHITECTURE.md` and `README.md` in the same change if you alter endpoints, ports, or tables.

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
2. Run `PYTHONPATH=app .venv/bin/pytest -v tests` and observe **all tests passing**.
3. If `docker-compose.yml` or `Dockerfile` changed, run `docker compose config -q` (on a Docker-enabled host).
