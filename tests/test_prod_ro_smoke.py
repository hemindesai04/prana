import os

import psycopg
import pytest

from prana.migrations import get_migration_status


@pytest.mark.skipif(
    not os.getenv("PRANA_RO_DATABASE_URL"),
    reason="PRANA_RO_DATABASE_URL not set; skipping prod read-only smoke test",
)
def test_prod_readonly_smoke():
    ro_url = os.environ["PRANA_RO_DATABASE_URL"]
    with psycopg.connect(ro_url) as conn:
        current, head, at_head = get_migration_status(conn, ro_url)
        assert at_head, f"Prod DB not at Alembic head: current={current}, head={head}"

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            assert cur.fetchone()[0] >= 0

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO backup_runs (status) VALUES ('smoke_test')")
        conn.rollback()
