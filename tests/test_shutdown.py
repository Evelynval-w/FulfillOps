"""SIGTERM: a request that is already running finishes, then the API exits cleanly."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import psycopg
import pytest
from stack import CLEAN_EXIT, MIGRATIONS, ROOT, call, free_port, wait_until

from fulfillops.common.migrate import run_migrations

# On Windows, Popen.send_signal(SIGTERM) is a hard kill, so a graceful shutdown cannot be tested
# there. Production runs on Linux, where this works.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is a hard kill on Windows")


def test_request_in_flight_finishes_then_api_exits_cleanly(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    port = free_port()
    env = {
        **os.environ,
        "DATABASE_URL": schema_url,
        "APP_ENV": "test",
        "PORT": str(port),
        "MIGRATIONS_DIR": str(MIGRATIONS),
        "PYTHONPATH": str(ROOT / "src"),
    }
    api = subprocess.Popen(
        [sys.executable, "-m", "fulfillops.api"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert wait_until(lambda: call(port, "GET", "/health/ready")[0] == 200), "API did not start"

        # Hold a lock on the stock row, so the order's UPDATE has to wait: that keeps the
        # request "in flight" for as long as we want.
        blocker = psycopg.connect(schema_url)
        blocker.execute("SELECT 1 FROM products WHERE sku = 'PART-200' FOR UPDATE")

        result = {}

        def place_order():
            payload = json.dumps(
                {"customer_id": "CUST-SIGTERM", "items": [{"sku": "PART-200", "quantity": 1}]}
            )
            result["answer"] = call(
                port,
                "POST",
                "/api/v1/orders",
                payload,
                {"Content-Type": "application/json", "Idempotency-Key": "sigterm-key-0001"},
            )

        thread = threading.Thread(target=place_order)
        thread.start()

        def request_is_waiting_on_the_lock():
            with psycopg.connect(schema_url, autocommit=True) as probe:
                return (
                    probe.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    ).fetchone()[0]
                    >= 1
                )

        assert wait_until(request_is_waiting_on_the_lock), "request never reached the database"

        api.send_signal(signal.SIGTERM)
        time.sleep(1.0)
        assert api.poll() is None, "API exited while a request was still running"
        with pytest.raises(OSError):  # no new connections are accepted any more
            call(port, "GET", "/health/live")

        blocker.rollback()  # let the order go through
        blocker.close()
        thread.join(timeout=15)

        assert result["answer"][0] == 202, result
        assert api.wait(timeout=20) in CLEAN_EXIT, "API did not shut down cleanly"
        with psycopg.connect(schema_url) as conn:
            assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
            stock = conn.execute(
                "SELECT available FROM products WHERE sku = 'PART-200'"
            ).fetchone()[0]
            assert stock == 11  # the in-flight order was fully committed
    finally:
        if api.poll() is None:
            api.kill()
        api.stdout.close()


def test_idle_api_exits_quickly(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    port = free_port()
    env = {
        **os.environ,
        "DATABASE_URL": schema_url,
        "APP_ENV": "test",
        "PORT": str(port),
        "MIGRATIONS_DIR": str(MIGRATIONS),
        "PYTHONPATH": str(ROOT / "src"),
    }
    api = subprocess.Popen(
        [sys.executable, "-m", "fulfillops.api"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_until(lambda: call(port, "GET", "/health/live")[0] == 200)
        started = time.time()
        api.send_signal(signal.SIGTERM)
        assert api.wait(timeout=15) in CLEAN_EXIT
        assert time.time() - started < 10
    finally:
        if api.poll() is None:
            api.kill()
