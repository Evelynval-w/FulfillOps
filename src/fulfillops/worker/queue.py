"""The queue operations on the jobs table (ADR-001 D4, D7, rules I4, I5, I7).

Every function is one short transaction. The database clock (now()) decides about
leases and retry times, so two workers never disagree because of clock drift.
A job is "owned" by (id, locked_by, attempts): a worker that lost its lease can never
change the job any more, because its attempts number no longer matches.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

# Due pending jobs, or claimed jobs whose lease ended (the worker died or was too slow).
# SKIP LOCKED lets two workers pick different rows without waiting for each other.
_CLAIM_SQL = """
WITH next AS (
    SELECT id FROM jobs
    WHERE attempts < max_attempts
      AND ((state = 'pending' AND next_run_at <= now())
        OR (state = 'claimed' AND lease_expires_at < now()))
    ORDER BY next_run_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs j
SET state = 'claimed', locked_by = %(worker)s, attempts = j.attempts + 1,
    lease_expires_at = now() + make_interval(secs => %(lease)s), updated_at = now()
FROM next
WHERE j.id = next.id
RETURNING j.id, j.order_id, j.attempts, j.max_attempts
"""

# A job whose lease ended after its last attempt (a worker that dies every time):
# it must not loop forever, so it becomes dead and the order failed. No restock.
_REAP_SQL = """
WITH expired AS (
    SELECT id FROM jobs
    WHERE state = 'claimed' AND lease_expires_at < now() AND attempts >= max_attempts
    FOR UPDATE SKIP LOCKED
), dead AS (
    UPDATE jobs j
    SET state = 'dead', locked_by = NULL, lease_expires_at = NULL, updated_at = now(),
        last_error = 'lease expired on the last attempt (worker crashed or was too slow)'
    FROM expired
    WHERE j.id = expired.id
    RETURNING j.order_id
)
UPDATE orders o SET status = 'failed', updated_at = now()
FROM dead
WHERE o.id = dead.order_id AND o.status <> 'completed'
RETURNING o.id
"""

# Guarded completion (rule I4): the job must still be ours, and the order must not
# already be completed. Both updates are in one transaction.
_DONE_JOB_SQL = """
UPDATE jobs
SET state = 'done', locked_by = NULL, lease_expires_at = NULL, last_error = NULL,
    updated_at = now()
WHERE id = %s AND state = 'claimed' AND locked_by = %s AND attempts = %s
RETURNING order_id
"""

_RETRY_JOB_SQL = """
UPDATE jobs
SET state = 'pending', locked_by = NULL, lease_expires_at = NULL, last_error = %s,
    next_run_at = now() + make_interval(secs => %s), updated_at = now()
WHERE id = %s AND state = 'claimed' AND locked_by = %s AND attempts = %s
RETURNING order_id
"""

_DEAD_JOB_SQL = """
UPDATE jobs
SET state = 'dead', locked_by = NULL, lease_expires_at = NULL, last_error = %s,
    updated_at = now()
WHERE id = %s AND state = 'claimed' AND locked_by = %s AND attempts = %s
RETURNING order_id
"""


@dataclass(frozen=True)
class Job:
    id: int
    order_id: str
    attempts: int  # includes this attempt
    max_attempts: int
    customer_id: str

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


def backoff_seconds(
    attempts: int, base: float, cap: float, rng: random.Random | None = None
) -> float:
    """Wait after a failed attempt: base * 2^(attempts-1), plus or minus 25 %, at most cap."""
    rng = rng or random
    raw = min(cap, base * (2 ** (attempts - 1)))
    return min(cap, raw * rng.uniform(0.75, 1.25))


def reap_exhausted(pool: ConnectionPool) -> int:
    """Mark jobs that ran out of attempts while claimed as dead. Returns how many."""
    with pool.connection() as conn, conn.transaction():
        return len(conn.execute(_REAP_SQL).fetchall())


def claim_job(pool: ConnectionPool, worker_id: str, lease_seconds: float) -> Job | None:
    """Atomically take one due job: lease, attempts + 1, order processing."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(_CLAIM_SQL, {"worker": worker_id, "lease": lease_seconds}).fetchone()
        if row is None:
            return None
        order = conn.execute(
            "UPDATE orders SET status = 'processing', updated_at = now() "
            "WHERE id = %s AND status NOT IN ('completed', 'failed') RETURNING customer_id",
            (row["order_id"],),
        ).fetchone()
        if order is None:
            # the order is already finished: close the job instead of working again
            conn.execute(
                "UPDATE jobs SET state = 'done', locked_by = NULL, lease_expires_at = NULL, "
                "updated_at = now() WHERE id = %s",
                (row["id"],),
            )
            return None
    return Job(
        row["id"], row["order_id"], row["attempts"], row["max_attempts"], order["customer_id"]
    )


def complete_job(pool: ConnectionPool, job: Job, worker_id: str) -> bool:
    """Finish the job and the order together. False means the lease was lost (nothing changed)."""
    with pool.connection() as conn, conn.transaction():
        done = conn.execute(_DONE_JOB_SQL, (job.id, worker_id, job.attempts)).fetchone()
        if done is None:
            return False
        conn.execute(
            "UPDATE orders SET status = 'completed', updated_at = now() "
            "WHERE id = %s AND status <> 'completed'",
            (job.order_id,),
        )
    return True


def fail_job(
    pool: ConnectionPool, job: Job, worker_id: str, error: str, delay_seconds: float
) -> str | None:
    """Record a failed attempt. Returns 'retry', 'dead', or None if the lease was lost."""
    error = error[:500]
    with pool.connection() as conn, conn.transaction():
        if job.is_last_attempt:
            row = conn.execute(_DEAD_JOB_SQL, (error, job.id, worker_id, job.attempts)).fetchone()
            if row is None:
                return None
            # failed, and the stock stays reserved: no automatic restock (rule I7)
            conn.execute(
                "UPDATE orders SET status = 'failed', updated_at = now() "
                "WHERE id = %s AND status <> 'completed'",
                (job.order_id,),
            )
            return "dead"
        row = conn.execute(
            _RETRY_JOB_SQL, (error, delay_seconds, job.id, worker_id, job.attempts)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE orders SET status = 'queued', updated_at = now() "
            "WHERE id = %s AND status = 'processing'",
            (job.order_id,),
        )
        return "retry"
