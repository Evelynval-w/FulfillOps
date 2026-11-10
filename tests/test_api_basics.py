"""Health, catalog, inventory, error format and metrics."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fulfillops.api.app import create_app
from fulfillops.common.migrate import run_migrations
from fulfillops.common.settings import load_settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def settings_for(url, **extra):
    env = {"DATABASE_URL": url, "APP_ENV": "test", "MIGRATIONS_DIR": str(MIGRATIONS), **extra}
    return load_settings(env)


@pytest.fixture
def client(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    with TestClient(create_app(settings_for(schema_url))) as c:
        yield c


def test_live_is_ok(client):
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_when_database_and_migrations_are_fine(client):
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready"}


def test_ready_can_be_forced_to_fail(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    with TestClient(create_app(settings_for(schema_url, FAIL_READINESS="true"))) as c:
        r = c.get("/health/ready")
        assert r.status_code == 503 and r.json() == {"status": "not_ready"}
        assert c.get("/health/live").status_code == 200


def test_live_survives_a_dead_database_and_ready_fails():
    dead = "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1"
    with TestClient(create_app(settings_for(dead))) as c:
        assert c.get("/health/live").status_code == 200
        r = c.get("/health/ready")
        assert r.status_code == 503 and r.json() == {"status": "not_ready"}
        r = c.get("/api/v1/catalog")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "nobody" not in r.text and "127.0.0.1" not in r.text


def test_ready_turns_true_once_the_background_migration_finishes(schema_url):
    with TestClient(create_app(settings_for(schema_url))) as c:  # no manual migration
        for _ in range(50):
            if c.get("/health/ready").status_code == 200:
                break
            time.sleep(0.1)
        assert c.get("/health/ready").status_code == 200


def test_catalog_lists_the_three_products(client):
    body = client.get("/api/v1/catalog").json()
    assert {p["sku"]: p["unit_price_cents"] for p in body["products"]} == {
        "KIT-100": 1299,
        "PART-200": 599,
        "PACK-300": 249,
    }


def test_inventory_returns_seed_stock(client):
    assert client.get("/api/v1/inventory/PACK-300").json() == {"sku": "PACK-300", "available": 5}


def test_unknown_sku_is_404_with_error_code(client):
    r = client.get("/api/v1/inventory/NOPE-1")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "SKU_NOT_FOUND", "message": "Unknown SKU"}}


def test_unknown_route_uses_the_same_error_format(client):
    r = client.get("/nothing-here")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_metrics_has_the_required_series_without_customer_data(client):
    client.get("/api/v1/inventory/KIT-100")
    client.get("/api/v1/inventory/PART-200")
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    for name in (
        "fulfillops_orders_created_total",
        "fulfillops_orders_failed_total",
        "fulfillops_worker_retries_total",
        "fulfillops_http_requests_total",
        "fulfillops_http_request_duration_seconds_bucket",
    ):
        assert name in r.text
    # the route template is the label, never the SKU that was asked for
    assert 'route="/api/v1/inventory/{sku}"' in r.text
    assert "PART-200" not in r.text and "KIT-100" not in r.text
