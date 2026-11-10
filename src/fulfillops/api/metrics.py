"""Prometheus metrics for the API.

Request counters and latency are measured in the API process. The order and
retry counters are read from the database when Prometheus scrapes, so both API
replicas report the same totals and nothing depends on a process restart.
"""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import CounterMetricFamily
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("fulfillops.api")

# jobs.attempts is counted at claim, so every attempt after the first is a retry
_TOTALS_SQL = """
SELECT
  (SELECT count(*) FROM orders),
  (SELECT count(*) FROM orders WHERE status = 'failed'),
  (SELECT coalesce(sum(attempts - 1), 0) FROM jobs WHERE attempts > 1)
"""


class DatabaseTotals:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def collect(self):
        try:
            with self._pool.connection(timeout=2.0) as conn:
                with conn.cursor(row_factory=tuple_row) as cur:
                    created, failed, retries = cur.execute(_TOTALS_SQL).fetchone()
        except Exception as exc:
            log.warning("order totals unavailable (%s)", type(exc).__name__)
            return
        for name, doc, value in (
            ("fulfillops_orders_created", "Orders accepted", created),
            ("fulfillops_orders_failed", "Orders that ended failed", failed),
            ("fulfillops_worker_retries", "Job retries done by workers", retries),
        ):
            family = CounterMetricFamily(name, doc)
            family.add_metric([], int(value))
            yield family


class Metrics:
    def __init__(self, pool: ConnectionPool):
        self.registry = CollectorRegistry()
        # labels are only method, route template and status: never ids or customer data
        self.requests = Counter(
            "fulfillops_http_requests",
            "HTTP requests handled by the API",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "fulfillops_http_request_duration_seconds",
            "HTTP request latency in seconds",
            ["method", "route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=self.registry,
        )
        self.registry.register(DatabaseTotals(pool))

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        self.requests.labels(method, route, str(status)).inc()
        self.latency.labels(method, route).observe(seconds)
