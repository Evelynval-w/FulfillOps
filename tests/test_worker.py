"""Worker: claim, guarded completion, retries, dead letter, crash recovery, two workers."""

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from fulfillops.api.orders import create_order, parse_order
from fulfillops.common.db import create_pool
from fulfillops.common.migrate import run_migrations
from fulfillops.common.settings import load_settings
from fulfillops.worker import queue
from fulfillops.worker.runner import Worker
from fulfillops.worker.settings import load_worker_settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


@pytest.fixture
def env(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    return {"DATABASE_URL": schema_url, "APP_ENV": "test", "PROCESSING_SECONDS": "0"}


@pytest.fixture
def pool(env):
    p = create_pool(load_settings(env))
    p.wait()
    yield p
    p.close()


def make_worker(pool, env, worker_id="w1", **overrides):
    s = load_worker_settings({**env, "WORKER_ID": worker_id})
    return Worker(pool, replace(s, **overrides), threading.Event())


def new_order(pool, key, customer="CUST-1", sku="PART-200", qty=1):
    raw = json.dumps({"customer_id": customer, "items": [{"sku": sku, "quantity": qty}]})
    return create_order(pool, key, parse_order(raw.encode())).order_id


def row(pool, sql, *args):
    with pool.connection() as conn:
        return conn.execute(sql, args).fetchone()


def order_status(pool, oid):
    return row(pool, "SELECT status FROM orders WHERE id = %s", oid)["status"]


def job_of(pool, oid):
    return row(pool, "SELECT * FROM jobs WHERE order_id = %s", oid)


def expire_lease(pool, oid):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE order_id = %s",
            (oid,),
        )


def make_due(pool, oid):
    with pool.connection() as conn:
        conn.execute("UPDATE jobs SET next_run_at = now() WHERE order_id = %s", (oid,))


# ---------- settings ----------


def test_poison_customer_is_ignored_in_production():
    base = {"DATABASE_URL": "postgresql://x", "POISON_CUSTOMER_ID": "POISON"}
    assert load_worker_settings({**base, "APP_ENV": "production"}).poison_customer_id is None
    assert load_worker_settings({**base, "APP_ENV": "test"}).poison_customer_id == "POISON"
    assert load_worker_settings(base).poison_customer_id is None  # default env is production


def test_backoff_is_2_4_8_16_with_jitter_and_cap():
    rng = random.Random(1)
    for attempt, nominal in [(1, 2), (2, 4), (3, 8), (4, 16)]:
        for _ in range(50):
            d = queue.backoff_seconds(attempt, 2.0, 30.0, rng)
            assert nominal * 0.75 <= d <= nominal * 1.25
    assert queue.backoff_seconds(10, 2.0, 30.0, rng) <= 30.0


# ---------- happy path ----------


def test_worker_completes_an_order(pool, env):
    oid = new_order(pool, "key-happy-0001")
    assert make_worker(pool, env).run_once() is True
    assert order_status(pool, oid) == "completed"
    assert job_of(pool, oid)["state"] == "done"
    assert make_worker(pool, env).run_once() is False  # nothing left


def test_claim_sets_lease_attempts_and_processing(pool, env):
    oid = new_order(pool, "key-claim-0001")
    job = queue.claim_job(pool, "w1", 10)
    assert (job.order_id, job.attempts) == (oid, 1)
    j = job_of(pool, oid)
    assert (j["state"], j["locked_by"], j["attempts"]) == ("claimed", "w1", 1)
    assert j["lease_expires_at"] is not None
    assert order_status(pool, oid) == "processing"
    assert queue.claim_job(pool, "w2", 10) is None  # leased: nobody else can take it


# ---------- crash recovery (hard gate 2) ----------


def test_job_is_reclaimed_after_the_lease_expires_and_completes_once(pool, env):
    oid = new_order(pool, "key-crash-0001")
    queue.claim_job(pool, "dead-worker", 10)  # claimed, then the worker "dies"
    assert queue.claim_job(pool, "w2", 10) is None  # lease still valid
    expire_lease(pool, oid)
    job = queue.claim_job(pool, "w2", 10)
    assert (job.order_id, job.attempts) == (oid, 2)
    assert queue.complete_job(pool, job, "w2") is True
    assert order_status(pool, oid) == "completed"


def test_a_worker_that_lost_its_lease_cannot_complete(pool, env):
    oid = new_order(pool, "key-fence-00001")
    slow = queue.claim_job(pool, "slow", 10)
    expire_lease(pool, oid)
    fast = queue.claim_job(pool, "fast", 10)
    assert queue.complete_job(pool, fast, "fast") is True
    # the slow worker wakes up and tries to finish the same order
    assert queue.complete_job(pool, slow, "slow") is False
    assert queue.fail_job(pool, slow, "slow", "late", 1) is None
    assert order_status(pool, oid) == "completed"
    assert job_of(pool, oid)["state"] == "done"


def test_old_attempt_of_the_same_worker_id_cannot_complete(pool, env):
    oid = new_order(pool, "key-fence-00002")
    first = queue.claim_job(pool, "w1", 10)
    expire_lease(pool, oid)
    second = queue.claim_job(pool, "w1", 10)  # same worker id, attempts is now 2
    assert queue.complete_job(pool, first, "w1") is False
    assert queue.complete_job(pool, second, "w1") is True


def test_worker_that_dies_on_every_attempt_ends_failed_not_looping(pool, env):
    oid = new_order(pool, "key-loop-00001")
    stock_after_order = row(pool, "SELECT available FROM products WHERE sku='PART-200'")[
        "available"
    ]
    for attempt in range(1, 6):
        job = queue.claim_job(pool, f"w{attempt}", 10)
        assert job.attempts == attempt
        expire_lease(pool, oid)  # the worker crashed
    assert queue.claim_job(pool, "w6", 10) is None  # attempts used up: no 6th claim
    assert queue.reap_exhausted(pool) == 1
    assert job_of(pool, oid)["state"] == "dead"
    assert order_status(pool, oid) == "failed"
    assert (
        row(pool, "SELECT available FROM products WHERE sku='PART-200'")["available"]
        == stock_after_order
    )


# ---------- retries and poison job (hard gate 2) ----------


def test_poison_job_retries_then_fails_without_restock(pool, env):
    oid = new_order(pool, "key-poison-0001", customer="POISON-1")
    stock = row(pool, "SELECT available FROM products WHERE sku='PART-200'")["available"]
    w = make_worker(pool, env, poison_customer_id="POISON-1", backoff_base_s=0.0)
    runs = 0
    while w.run_once():
        runs += 1
        assert runs <= 5, "infinite retry loop"
    assert runs == 5
    j = job_of(pool, oid)
    assert (j["state"], j["attempts"]) == ("dead", 5)
    assert "PoisonJobError" in j["last_error"]
    assert order_status(pool, oid) == "failed"
    assert row(pool, "SELECT available FROM products WHERE sku='PART-200'")["available"] == stock
    assert make_worker(pool, env).run_once() is False  # stays dead


def test_failed_attempt_goes_back_to_pending_after_a_delay(pool, env):
    oid = new_order(pool, "key-retry-00001", customer="POISON-1")
    w = make_worker(pool, env, poison_customer_id="POISON-1", backoff_base_s=60.0)
    w.run_once()
    j = job_of(pool, oid)
    assert (j["state"], j["attempts"], j["locked_by"]) == ("pending", 1, None)
    assert order_status(pool, oid) == "queued"
    assert w.run_once() is False  # next_run_at is about a minute away
    make_due(pool, oid)
    assert w.run_once() is True
    assert job_of(pool, oid)["attempts"] == 2


def test_retry_metric_source_counts_attempts_after_the_first(pool, env):
    new_order(pool, "key-metric-0001", customer="POISON-1")
    w = make_worker(pool, env, poison_customer_id="POISON-1", backoff_base_s=0.0)
    while w.run_once():
        pass
    totals = row(pool, "SELECT coalesce(sum(attempts - 1), 0) AS r FROM jobs WHERE attempts > 1")
    assert int(totals["r"]) == 4


# ---------- two workers ----------


def test_two_workers_complete_every_order_exactly_once(pool, env):
    ids = [new_order(pool, f"key-two-{i:05d}", sku="PART-200") for i in range(10)]
    completions = []
    lock = threading.Lock()

    def drain(name):
        w = make_worker(pool, env, name)
        n = 0
        while w.run_once():
            n += 1
        with lock:
            completions.append(n)

    with ThreadPoolExecutor(2) as ex:
        list(ex.map(drain, ["w1", "w2"]))
    assert sum(completions) == 10  # each job was handled by exactly one worker
    for oid in ids:
        assert order_status(pool, oid) == "completed"
        assert job_of(pool, oid)["attempts"] == 1


# ---------- shutdown ----------


def test_sigterm_finishes_the_current_job_then_stops(pool, env):
    oid = new_order(pool, "key-term-00001")
    w = make_worker(pool, env, processing_seconds=0.5)
    t = threading.Thread(target=w.run_forever)
    t.start()
    time.sleep(0.2)  # the job is being processed
    w.stop.set()  # what the SIGTERM handler does
    t.join(timeout=5)
    assert not t.is_alive()
    assert order_status(pool, oid) == "completed"


# ---------- starting before the API has migrated ----------


def test_worker_started_before_migration_logs_once_then_recovers(schema_url, caplog):
    caplog.set_level("INFO", logger="fulfillops.worker")
    env = {"DATABASE_URL": schema_url, "APP_ENV": "test", "PROCESSING_SECONDS": "0"}
    p = create_pool(load_settings(env))
    p.wait()
    try:
        w = make_worker(p, env, poll_interval_s=0.05)  # schema is still empty: no tables
        t = threading.Thread(target=w.run_forever)
        t.start()
        time.sleep(1.5)  # many failed loops
        cannot = [r for r in caplog.records if "cannot work yet" in r.getMessage()]
        assert len(cannot) == 1, "the same error must be logged once, not on every loop"
        assert "UndefinedTable" in cannot[0].getMessage()

        run_migrations(schema_url, MIGRATIONS)  # the API finally migrates
        oid = new_order(p, "key-late-migr-01")
        deadline = time.time() + 10
        while order_status(p, oid) != "completed" and time.time() < deadline:
            time.sleep(0.1)
        w.stop.set()
        t.join(timeout=5)
        assert order_status(p, oid) == "completed"
        assert any("recovered" in r.getMessage() for r in caplog.records)
    finally:
        p.close()
