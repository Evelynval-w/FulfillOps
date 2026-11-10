"""Applies the numbered .sql files in db/migrations, each one only once.

Run it by hand with: python -m fulfillops.common.migrate
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
from psycopg.rows import tuple_row

from fulfillops.common.settings import load_settings

# every replica must use the same number
ADVISORY_LOCK_KEY = 7_204_001

_FILE_PATTERN = re.compile(r"^\d{3}_[A-Za-z0-9_]+\.sql$")

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def discover(migrations_dir: Path) -> list[Path]:
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations folder not found: {migrations_dir}")
    return sorted(p for p in migrations_dir.iterdir() if _FILE_PATTERN.match(p.name))


def run_migrations(database_url: str, migrations_dir: Path) -> list[str]:
    """Apply the pending files and return the versions applied by this call."""
    files = discover(migrations_dir)
    applied_now: list[str] = []
    # the lock belongs to the connection, so use one dedicated connection
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(_CREATE_TRACKING_TABLE)
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in files:
                version = path.stem
                if version in done:
                    continue
                # one transaction per file, with its schema_migrations row
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    return applied_now


def schema_is_current(conn: psycopg.Connection, migrations_dir: Path) -> bool:
    """True if every migration file is already recorded (used by /health/ready)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
        if not cur.fetchone()[0]:
            return False
        cur.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in cur.fetchall()}
    return {p.stem for p in discover(migrations_dir)} <= done


def main() -> None:
    settings = load_settings()
    applied = run_migrations(settings.database_url, settings.migrations_dir)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")


if __name__ == "__main__":
    main()
