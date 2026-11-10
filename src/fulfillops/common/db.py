"""Database pool shared by the API and the worker.

Use it like this: a request borrows one connection, and the transaction
commits when the block ends or rolls back if something fails.

    with pool.connection() as conn:
        conn.execute(...)
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from fulfillops.common.settings import Settings

POOL_TIMEOUT_SECONDS = 10.0  # how long a request waits for a free connection


def create_pool(settings: Settings) -> ConnectionPool:
    pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=min(2, settings.pool_size),
        max_size=settings.pool_size,
        timeout=POOL_TIMEOUT_SECONDS,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    # don't wait for the database, so the API still starts when it is down
    pool.open(wait=False)
    return pool


def database_is_reachable(pool: ConnectionPool, timeout: float = 2.0) -> bool:
    try:
        with pool.connection(timeout=timeout) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
