import os
from typing import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from prana.config import Settings
from prana.db import bootstrap_users
from prana.main import create_app
from prana.migrations import run_migrations


def _test_db_url() -> str:
    return os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://prana:prana_test@localhost:5432/prana_test",
    )


@pytest.fixture(scope="session")
def migrated_db() -> str:
    url = _test_db_url()
    run_migrations(url)
    return url


@pytest.fixture()
def db_conn(migrated_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE users, raw_ingestions, biomarkers, workouts, ingestion_issues, backup_runs
                RESTART IDENTITY CASCADE
                """
            )
        conn.commit()
        bootstrap_users(conn, [("hemin", "hemin-secret")], default_tz="America/Los_Angeles")
        yield conn


@pytest.fixture()
def client(migrated_db: str, db_conn: psycopg.Connection) -> Iterator[TestClient]:
    settings = Settings(
        database_url=migrated_db,
        admin_token="admin-secret",
        bootstrap_users=[],
        backup_dir="/tmp/prana-test-backups",
        backup_cron_hour=3,
        backup_retention_days=30,
        timezone="America/Los_Angeles",
        max_request_bytes=1024 * 1024,
        run_migrations_on_startup=False,
    )
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app) as tc:
        yield tc
