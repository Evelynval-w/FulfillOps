"""API, PostgreSQL and real worker processes together (nothing is mocked or called in-process)."""

import json
import re

import psycopg
import pytest
from stack import MIGRATIONS, Stack, wait_until

from fulfillops.common.migrate import run_migrations

FAST_WORKER = {"PROCESSING_SECONDS": 0, "POLL_INTERVAL_S": 0.1, "LEASE_SECONDS": 2}


@pytest.fixture
def stack(schema_url, tmp_path):
    run_migrations(schema_url, MIGRATIONS)
    s = Stack(schema_url, tmp_path)
    s.url = schema_url
    yield s
    s.stop_all()


def db(stack, sql, *args):
    with psycopg.connect(stack.url) as conn:
        return conn.execute(sql, args).fetchone()


def order_status(stack, order_id):
    status, body = stack.get(f"/api/v1/orders/{order_id}")
    assert status == 200, body
    return json.loads(body)["status"]


def wait_status(stack, order_id, wanted, seconds=20):
    ok = wait_until(lambda: order_status(stack, order_id) == wanted, seconds)
    assert ok, f"order never became {wanted}\n" + stack.logs()


def metric(stack, name):
    text = stack.get("/metrics")[1].decode()
    found = re.search(rf"^{name}(?:_total)? ([0-9.]+)$", text, re.M)
    assert found, f"{name} missing from /metrics"
    return float(found.group(1))


def stock(stack, sku="PART-200"):
    return db(stack, "SELECT available FROM products WHERE sku = %s", sku)[0]


def test_order_is_accepted_without_a_worker_and_completed_once_one_starts(stack):
    stack.start_api()
    code, body = stack.post_order("integration-key-0001")
    assert code == 202, body
    order_id = json.loads(body)["order_id"]

    # no worker yet: the API has already answered and the stock is already reserved
    assert order_status(stack, order_id) == "queued"
    assert stock(stack) == 11

    stack.start_worker("worker-a", **FAST_WORKER)
    wait_status(stack, order_id, "completed")

    # a retry of the same request, after completion, changes nothing
    code, body = stack.post_order("integration-key-0001")
    assert (code, json.loads(body)["order_id"]) == (202, order_id)
    assert stock(stack) == 11
    assert metric(stack, "fulfillops_orders_created") == 1
    assert metric(stack, "fulfillops_orders_failed") == 0


def test_worker_killed_mid_job_is_recovered_by_another_worker(stack):
    stack.start_api()
    # worker A is slow on purpose, so we can kill it while it holds the job
    slow = {**FAST_WORKER, "PROCESSING_SECONDS": 60}
    worker_a = stack.start_worker("worker-a", **slow)
    order_id = json.loads(stack.post_order("integration-key-0002")[1])["order_id"]

    def held_by_a():
        row = db(stack, "SELECT state, locked_by FROM jobs WHERE order_id = %s", order_id)
        return row == ("claimed", "worker-a")

    assert wait_until(held_by_a), "worker A never took the job\n" + stack.logs()
    worker_a.kill()  # SIGKILL: no cleanup, like a crashed pod
    worker_a.wait()

    stack.start_worker("worker-b", **FAST_WORKER)
    wait_status(stack, order_id, "completed", seconds=30)

    state, attempts = db(stack, "SELECT state, attempts FROM jobs WHERE order_id = %s", order_id)
    assert (state, attempts) == ("done", 2)  # one crashed attempt, one that finished
    assert stock(stack) == 11  # reserved once, not twice
    assert metric(stack, "fulfillops_worker_retries") == 1


def test_poison_order_ends_failed_keeps_its_stock_and_does_not_block_others(stack):
    stack.start_api()
    poison = {
        **FAST_WORKER,
        "POISON_CUSTOMER_ID": "CUST-POISON",
        "BACKOFF_BASE_S": 0.1,
        "BACKOFF_CAP_S": 0.3,
    }
    stack.start_worker("worker-a", **poison)

    bad = json.loads(stack.post_order("integration-key-0003", "CUST-POISON", quantity=2)[1])
    good = json.loads(stack.post_order("integration-key-0004", "CUST-OK", quantity=1)[1])

    wait_status(stack, good["order_id"], "completed")
    wait_status(stack, bad["order_id"], "failed", seconds=30)

    state, attempts = db(
        stack, "SELECT state, attempts FROM jobs WHERE order_id = %s", bad["order_id"]
    )
    assert (state, attempts) == ("dead", 5)
    assert stock(stack) == 12 - 2 - 1  # the failed order's stock is not given back
    assert metric(stack, "fulfillops_orders_failed") == 1
    assert metric(stack, "fulfillops_worker_retries") == 4


def test_readiness_follows_the_flag_but_liveness_stays_up(stack):
    stack.start_api(FAIL_READINESS="true")
    assert stack.get("/health/ready")[0] == 503
    assert stack.get("/health/live")[0] == 200
