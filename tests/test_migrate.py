"""Migration tests. They need a throwaway database: set DATABASE_URL to it."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from fulfillops.common.migrate import discover, run_migrations, schema_is_current

ADMIN_URL = os.environ.get("DATABASE_URL")
MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="DATABASE_URL (test database) not set")


def stock(url):
    with psycopg.connect(url) as conn:
        return dict(conn.execute("SELECT sku, available FROM products").fetchall())


def test_files_are_discovered_in_order():
    assert [p.stem for p in discover(MIGRATIONS)] == ["001_init", "002_seed_catalog"]


def test_applies_everything_and_seeds_stock(schema_url):
    applied = run_migrations(schema_url, MIGRATIONS)
    assert applied == ["001_init", "002_seed_catalog"]
    assert stock(schema_url) == {"KIT-100": 7, "PART-200": 12, "PACK-300": 5}


def test_second_run_does_nothing_and_keeps_spent_stock(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    with psycopg.connect(schema_url, autocommit=True) as conn:
        conn.execute("UPDATE products SET available = available - 2 WHERE sku = 'KIT-100'")
    assert run_migrations(schema_url, MIGRATIONS) == []
    assert stock(schema_url)["KIT-100"] == 5  # a restart never recreates spent stock


def test_schema_is_current_reports_the_state(schema_url):
    with psycopg.connect(schema_url) as conn:
        assert schema_is_current(conn, MIGRATIONS) is False
    run_migrations(schema_url, MIGRATIONS)
    with psycopg.connect(schema_url) as conn:
        assert schema_is_current(conn, MIGRATIONS) is True


def test_replicas_starting_together_apply_each_migration_once(schema_url):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run_migrations(schema_url, MIGRATIONS), range(4)))
    assert sorted(v for r in results for v in r) == ["001_init", "002_seed_catalog"]
    with psycopg.connect(schema_url) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2


def test_failed_migration_leaves_no_trace(schema_url, tmp_path):
    (tmp_path / "001_bad.sql").write_text("CREATE TABLE ok (id int); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        run_migrations(schema_url, tmp_path)
    with psycopg.connect(schema_url) as conn:
        assert conn.execute("SELECT to_regclass('ok')").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
