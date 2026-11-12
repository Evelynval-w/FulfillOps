"""scripts/reset_test_data.py: wipes orders only in the test environment, restores seed stock."""

import os
import subprocess
import sys

import psycopg
from stack import MIGRATIONS, ROOT

from fulfillops.api.orders import create_order, parse_order
from fulfillops.common.db import create_pool
from fulfillops.common.migrate import run_migrations
from fulfillops.common.settings import load_settings

SCRIPT = ROOT / "scripts" / "reset_test_data.py"


def run(url, app_env, *args):
    env = {**os.environ, "DATABASE_URL": url, "APP_ENV": app_env}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
    )


def one(url, sql):
    with psycopg.connect(url) as conn:
        return conn.execute(sql).fetchone()[0]


def place_order(url):
    env = {"DATABASE_URL": url, "APP_ENV": "test"}
    pool = create_pool(load_settings(env))
    pool.wait()
    try:
        body = b'{"customer_id": "C1", "items": [{"sku": "PART-200", "quantity": 3}]}'
        create_order(pool, "reset-script-key-01", parse_order(body))
    finally:
        pool.close()


def test_reset_restores_seed_stock_and_removes_orders(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    seed = one(schema_url, "SELECT json_object_agg(sku, available) FROM products")
    place_order(schema_url)
    assert one(schema_url, "SELECT count(*) FROM orders") == 1
    assert one(schema_url, "SELECT available FROM products WHERE sku = 'PART-200'") == 9

    result = run(schema_url, "test", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert one(schema_url, "SELECT count(*) FROM orders") == 0
    assert one(schema_url, "SELECT count(*) FROM jobs") == 0
    # the hard-coded numbers in the script must equal what the migration seeds
    assert one(schema_url, "SELECT json_object_agg(sku, available) FROM products") == seed


def test_it_refuses_outside_the_test_environment_and_without_yes(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    place_order(schema_url)

    for app_env, args in (("production", ("--yes",)), ("development", ("--yes",)), ("test", ())):
        result = run(schema_url, app_env, *args)
        assert result.returncode == 1, (app_env, args)
        assert one(schema_url, "SELECT count(*) FROM orders") == 1  # nothing was deleted
