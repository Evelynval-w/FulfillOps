"""Shared database access for the API and the worker (decision D2: one thin helper module).

Sync psycopg with a connection pool (decision: sync, plain SQL, no ORM). A request borrows one
connection, runs its whole transaction, and gives it back:

    with pool.connection() as conn:      # commits on success, rolls back on any exception
        conn.execute(...)

The pool opens WITHOUT waiting for the database, so the process starts (and /health/live
answers) even when PostgreSQL is down; the pool keeps reconnecting in the background.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from fulfillops.common.settings import Settings

# How long a request waits for a free connection before failing (seconds).
POOL_TIMEOUT_SECONDS = 10.0


def create_pool(settings: Settings) -> ConnectionPool:
    pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=min(2, settings.pool_size),
        max_size=settings.pool_size,
        timeout=POOL_TIMEOUT_SECONDS,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    pool.open(wait=False)
    return pool


def database_is_reachable(pool: ConnectionPool, timeout: float = 2.0) -> bool:
    """True if a connection can be borrowed and answers a trivial query."""
    try:
        with pool.connection(timeout=timeout) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # any failure means "not reachable"; readiness reports it as 503
        return False
