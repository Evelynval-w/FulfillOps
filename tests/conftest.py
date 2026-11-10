"""Shared test setup: a throwaway schema inside the test database."""

import os
import uuid

import psycopg
import pytest

ADMIN_URL = os.environ.get("DATABASE_URL")


@pytest.fixture
def schema_url():
    """A connection URL that points at a fresh, empty schema (dropped afterwards)."""
    if not ADMIN_URL:
        pytest.skip("DATABASE_URL (test database) not set")
    name = "t_" + uuid.uuid4().hex[:8]
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {name}")
        sep = "&" if "?" in ADMIN_URL else "?"
        yield f"{ADMIN_URL}{sep}options=-csearch_path%3D{name}"
        admin.execute(f"DROP SCHEMA {name} CASCADE")
