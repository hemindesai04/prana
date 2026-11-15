# AGENTS.md — Guidelines for Coding Agents

This file defines the non-negotiable rules, module boundaries, and verification gates for any AI agent or developer working in `prana`. Read `ARCHITECTURE.md` and `IMPLEMENTATION.md` before making changes.

---

## 1. Project Overview
`prana` is a self-hosted, multi-user, containerized health biomarker hub designed to run on a **Raspberry Pi 4 (ARM64)** using **Docker Compose**. It ingests data from Apple Watch (via Health Auto Export), Oura Ring 4 (via local BLE, no subscription), and future wearables into PostgreSQL, with planned AI trend analysis and proactive notifications.

---

## 2. Repository Map
- `ARCHITECTURE.md` — System modules, container topology, HTTP/SQL contracts.
- `IMPLEMENTATION.md` — Phased roadmap and acceptance criteria.
- `docker-compose.yml` — Container definitions, port mappings (`9xxx` series), log rotation.
- `db/init/01_schema.sql` — PostgreSQL multi-user schema (`users`, `raw_ingestions`, `biomarkers`, `workouts`).
- `app/prana/` — Modular Python application (`config.py`, `logging_setup.py`, `db.py`, `ingestor/`, `maintenance/`).
- `tests/` — Unit and end-to-end flow tests (`pytest`).

---

## 3. Strict Do's and Don'ts

### DO
- **Keep everything containerized:** All services, schedulers, and backup jobs must run inside Docker (`docker-compose.yml`).
- **Respect module boundaries:**
  - Connectors (Apple Health, Oura BLE, future wearables) communicate with the core app **only** via the HTTP ingest contracts or internal module interfaces defined in `ARCHITECTURE.md`.
  - Keep ingestion, storage, maintenance, analysis, and notification modules decoupled.
- **Preserve multi-user isolation:** Every ingestion, query, and analysis path must scope data by `user_id`. Never create single-user global tables or bypass token-to-user resolution.
- **Preserve raw data:** Always persist raw payloads in `raw_ingestions` (`JSONB`) before or alongside normalization.
- **Write unit AND flow tests for every change:** Every new feature, bugfix, or endpoint must include unit tests and flow tests covering the scope of the change.
- **Emit structured JSON logs:** Use `prana.logging_setup.get_logger(__name__)` so all logs go to `stdout` as structured JSON.
- **Update docs when contracts change:** If you modify an API endpoint, DB table, or container port, update `ARCHITECTURE.md` and `README.md` in the same change.

### DON'T
- **Never delete data, volumes, or drop tables:** Never run `docker compose down -v`, `rm -rf pgdata`, `DROP TABLE`, or destructive SQL without explicit user confirmation.
- **Never install runtime dependencies directly on the Pi host:** Do not instruct or script bare-metal `pip install`, `apt install`, or host `crontab` entries for app logic—put it in Docker.
- **Never commit secrets or personal health data:** Never commit `.env`, API tokens, `auth_key`s, `backups/`, or exported `export.xml` / JSON health dumps.
- **Never hardcode `8xxx` ports:** Port `8xxx` is reserved (e.g., Home Assistant). Prana uses the **`9xxx`** series (`9000` for API, `9300` for Grafana).
- **Never bypass tests or report unverified passes:** Do not mark a task complete without running `pytest` and verifying all tests pass after the final edit.

---

## 4. Mandatory Verification Checklist
Before finishing any task, run and observe passing output for:
1. **Syntax & Tests:**
   ```bash
   pytest -v
   ```
2. **Compose Validation (when `docker-compose.yml` or `Dockerfile` changes):**
   ```bash
   docker compose config -q
   ```
3. **Runtime Healthcheck (when running containers):**
   ```bash
   curl -fsS http://localhost:9000/health
   ```
