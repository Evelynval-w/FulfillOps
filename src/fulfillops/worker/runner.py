"""The worker loop: claim a job, do the work, complete it or schedule a retry."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from psycopg_pool import ConnectionPool

from fulfillops.common.db import create_pool
from fulfillops.worker import queue
from fulfillops.worker.settings import WorkerSettings

log = logging.getLogger("fulfillops.worker")

MAX_ERROR_WAIT_S = 5.0  # longest wait between tries while the database or schema is not ready


class PoisonJobError(RuntimeError):
    """Raised on purpose for the reserved poison customer (test and development only)."""


class Worker:
    def __init__(self, pool: ConnectionPool, settings: WorkerSettings, stop: threading.Event):
        self.pool = pool
        self.s = settings
        self.stop = stop

    def do_work(self, job: queue.Job) -> None:
        """The business work. Simulated: there is no real fulfilment system in this project."""
        if self.s.poison_customer_id and job.customer_id == self.s.poison_customer_id:
            raise PoisonJobError("poison job: this order always fails")
        # SIGTERM does not interrupt this: the job is finished first, then the loop stops
        time.sleep(self.s.processing_seconds)

    def run_once(self) -> bool:
        """Handle at most one job. Returns True if a job was claimed."""
        reaped = queue.reap_exhausted(self.pool)
        if reaped:
            log.warning(
                "worker=%s reaped %d job(s) that ran out of attempts", self.s.worker_id, reaped
            )
        job = queue.claim_job(self.pool, self.s.worker_id, self.s.lease_seconds)
        if job is None:
            return False
        log.info(
            "worker=%s order=%s job=%d attempt=%d/%d claimed",
            self.s.worker_id,
            job.order_id,
            job.id,
            job.attempts,
            job.max_attempts,
        )
        try:
            self.do_work(job)
        except Exception as exc:
            delay = queue.backoff_seconds(job.attempts, self.s.backoff_base_s, self.s.backoff_cap_s)
            outcome = queue.fail_job(
                self.pool, job, self.s.worker_id, f"{type(exc).__name__}: {exc}", delay
            )
            log.warning(
                "worker=%s order=%s job=%d attempt=%d/%d failed (%s) -> %s%s",
                self.s.worker_id,
                job.order_id,
                job.id,
                job.attempts,
                job.max_attempts,
                type(exc).__name__,
                outcome or "lease lost",
                f" in {delay:.1f}s" if outcome == "retry" else "",
            )
            return True
        if queue.complete_job(self.pool, job, self.s.worker_id):
            log.info("worker=%s order=%s job=%d completed", self.s.worker_id, job.order_id, job.id)
        else:
            log.warning(
                "worker=%s order=%s job=%d lease lost, result discarded",
                self.s.worker_id,
                job.order_id,
                job.id,
            )
        return True

    def run_forever(self) -> None:
        log.info("worker=%s started", self.s.worker_id)
        failing: str | None = None  # error type already logged, so a long outage is one line
        failures = 0
        while not self.stop.is_set():
            try:
                busy = self.run_once()
            except Exception as exc:  # database down, schema not ready yet, etc
                kind = type(exc).__name__
                failures += 1
                if kind != failing:
                    failing = kind
                    log.error(
                        "worker=%s cannot work yet (%s); the schema may not be migrated or the "
                        "database may be down. Waiting and backing off, not logging this again",
                        self.s.worker_id,
                        kind,
                    )
                # back off (1 s, 2 s, 4 s ... up to MAX_ERROR_WAIT_S): do not hammer a sick database
                self.stop.wait(
                    min(MAX_ERROR_WAIT_S, self.s.poll_interval_s * 2 ** min(failures, 8))
                )
                continue
            if failing is not None:
                log.info("worker=%s recovered after %d failed loop(s)", self.s.worker_id, failures)
                failing, failures = None, 0
            if not busy:
                self.stop.wait(self.s.poll_interval_s)
        log.info("worker=%s stopped", self.s.worker_id)


def run(settings: WorkerSettings, stop: threading.Event) -> None:
    pool = create_pool(replace(settings.base, pool_size=settings.pool_size))
    try:
        Worker(pool, settings, stop).run_forever()
    finally:
        pool.close()
