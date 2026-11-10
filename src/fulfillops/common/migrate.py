"""Migration runner: applies db/migrations/NNN_name.sql files in order, exactly once.

Several API replicas may start at the same moment. A PostgreSQL advisory lock makes them take
turns: the first replica applies the pending files, the others wait, then find nothing to do.
Each file runs in its own transaction together with its row in schema_migrations, so a failed
file leaves no half-applied schema.

Run by hand:  python -m fulfillops.common.migrate
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
from psycopg.rows import tuple_row

from fulfillops.common.settings import load_settings

# Any constant works, as long as every replica uses the same one.
ADVISORY_LOCK_KEY = 7_204_001

_FILE_PATTERN = re.compile(r"^\d{3}_[A-Za-z0-9_]+\.sql$")

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def discover(migrations_dir: Path) -> list[Path]:
    """Numbered .sql files in order. Other files (such as .gitkeep) are ignored."""
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations folder not found: {migrations_dir}")
    return sorted(p for p in migrations_dir.iterdir() if _FILE_PATTERN.match(p.name))


def run_migrations(database_url: str, migrations_dir: Path) -> list[str]:
    """Apply every pending migration. Returns the versions applied by THIS call."""
    files = discover(migrations_dir)
    applied_now: list[str] = []
    # A dedicated autocommit connection: the lock belongs to the session, and each
    # migration opens its own explicit transaction below.
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(_CREATE_TRACKING_TABLE)
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in files:
                version = path.stem
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    return applied_now


def schema_is_current(conn: psycopg.Connection, migrations_dir: Path) -> bool:
    """True if every migration file is recorded in schema_migrations (used by /health/ready)."""
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
