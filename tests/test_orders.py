"""Orders: validation, stock, idempotency and races."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from fulfillops.api.app import create_app
from fulfillops.api.errors import ApiError
from fulfillops.api.orders import create_order, parse_order
from fulfillops.common.db import create_pool
from fulfillops.common.migrate import run_migrations
from fulfillops.common.settings import load_settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def body(items, customer="CUST-042"):
    return {"customer_id": customer, "items": items}


def line(sku="KIT-100", quantity=1):
    return {"sku": sku, "quantity": quantity}


# ---------- validation (no database needed) ----------


def code_of(raw: bytes):
    with pytest.raises(ApiError) as caught:
        parse_order(raw)
    return caught.value.status, caught.value.code


def test_invalid_json():
    assert code_of(b"{not json") == (400, "INVALID_JSON")
    assert code_of(b"\xff\xfe") == (400, "INVALID_JSON")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"items": [line()]},
        {"customer_id": "", "items": [line()]},
        {"customer_id": "x" * 65, "items": [line()]},
        {"customer_id": 7, "items": [line()]},
        {"customer_id": "C", "items": []},
        {"customer_id": "C", "items": "nope"},
        {"customer_id": "C", "items": [line()] * 11},
        {"customer_id": "C", "items": ["KIT-100"]},
        {"customer_id": "C", "items": [{"sku": "KIT-100"}]},
    ],
)
def test_structure_errors_are_400(payload):
    assert code_of(json.dumps(payload).encode()) == (400, "INVALID_REQUEST")


@pytest.mark.parametrize("quantity", [0, -1, 21, 1.5, "2", True, None])
def test_bad_quantity_is_422(quantity):
    raw = json.dumps(body([line(quantity=quantity)])).encode()
    assert code_of(raw) == (422, "INVALID_QUANTITY")


def test_body_over_16_kib_is_400():
    raw = json.dumps(body([line()], customer="C" * 17000)).encode()
    assert code_of(raw) == (400, "INVALID_REQUEST")


def test_lines_with_the_same_sku_are_added_and_order_does_not_matter():
    a = parse_order(
        json.dumps(body([line("PACK-300", 1), line("KIT-100", 2), line("PACK-300", 3)])).encode()
    )
    b = parse_order(
        json.dumps(
            {"items": [line("KIT-100", 2), line("PACK-300", 4)], "customer_id": "CUST-042"}
        ).encode()
    )
    assert a.lines == (("KIT-100", 2), ("PACK-300", 4))
    assert a.payload_hash() == b.payload_hash()


# ---------- HTTP and database ----------


@pytest.fixture
def client(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    env = {"DATABASE_URL": schema_url, "APP_ENV": "test", "MIGRATIONS_DIR": str(MIGRATIONS)}
    with TestClient(create_app(load_settings(env))) as c:
        c.db_url = schema_url
        yield c


def post(client, payload, key="key-00000001", raw=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key
    data = raw if raw is not None else json.dumps(payload)
    return client.post("/api/v1/orders", content=data, headers=headers)


def stock(client, sku):
    return client.get(f"/api/v1/inventory/{sku}").json()["available"]


def test_accepts_an_order_reserves_stock_and_can_be_read_back(client):
    r = post(client, body([line("KIT-100", 1), line("PART-200", 2)]))
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert (stock(client, "KIT-100"), stock(client, "PART-200")) == (6, 10)
    got = client.get(f"/api/v1/orders/{r.json()['order_id']}").json()
    assert got == {
        "order_id": r.json()["order_id"],
        "status": "queued",
        "total_cents": 1299 + 2 * 599,
    }
    with psycopg.connect(client.db_url) as conn:  # the job is created with the order
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_missing_or_bad_key(client):
    r = post(client, body([line()]), key=None)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "IDEMPOTENCY_REQUIRED")
    assert post(client, body([line()]), key="short").status_code == 400
    assert stock(client, "KIT-100") == 7


def test_key_is_checked_before_the_body(client):
    r = post(client, None, key=None, raw="{broken")
    assert r.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


def test_invalid_json_and_unknown_sku_over_http(client):
    assert post(client, None, raw="{broken").json()["error"]["code"] == "INVALID_JSON"
    r = post(client, body([line("NOPE-1")]))
    assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_SKU")
    assert stock(client, "KIT-100") == 7


def test_body_over_16_kib_over_http(client):
    r = post(client, body([line()], customer="C" * 17000))
    assert (r.status_code, r.json()["error"]["code"]) == (400, "INVALID_REQUEST")


def test_duplicate_lines_are_added_together(client):
    r = post(client, body([line("PACK-300", 1), line("PACK-300", 3)]))
    assert r.status_code == 202
    assert stock(client, "PACK-300") == 1
    assert client.get(f"/api/v1/orders/{r.json()['order_id']}").json()["total_cents"] == 4 * 249


def test_replay_returns_the_same_order_and_reserves_once(client):
    first = post(client, body([line("PACK-300", 2)]))
    again = post(client, body([line("PACK-300", 2)]))
    assert again.status_code == 202 and again.json() == first.json()
    assert stock(client, "PACK-300") == 3


def test_replay_ignores_key_order_and_line_splitting(client):
    first = post(client, body([line("KIT-100", 1), line("PART-200", 2)]))
    other = (
        '{"items":[{"quantity":2,"sku":"PART-200"},{"quantity":1,"sku":"KIT-100"}],'
        '"customer_id":"CUST-042"}'
    )
    again = post(client, None, raw=other)
    assert again.status_code == 202 and again.json() == first.json()


def test_same_key_with_a_different_body_is_409(client):
    post(client, body([line("PACK-300", 1)]))
    r = post(client, body([line("PACK-300", 2)]))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    assert stock(client, "PACK-300") == 4


def test_out_of_stock_changes_nothing_and_frees_the_key(client):
    r = post(client, body([line("KIT-100", 1), line("PACK-300", 6)]))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "OUT_OF_STOCK")
    assert (stock(client, "KIT-100"), stock(client, "PACK-300")) == (7, 5)  # no partial reservation
    with psycopg.connect(client.db_url) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    ok = post(client, body([line("PACK-300", 5)]))  # same key, now a valid order
    assert ok.status_code == 202 and stock(client, "PACK-300") == 0


def test_unknown_order_is_404(client):
    r = client.get("/api/v1/orders/does-not-exist")
    assert (r.status_code, r.json()["error"]["code"]) == (404, "ORDER_NOT_FOUND")


def test_orders_created_metric_counts_accepted_orders(client):
    post(client, body([line()]))
    post(client, body([line()]))  # replay, not a new order
    assert "fulfillops_orders_created_total 1.0" in client.get("/metrics").text


# ---------- races (these call the transaction directly, from many threads) ----------


@pytest.fixture
def pool(schema_url):
    run_migrations(schema_url, MIGRATIONS)
    env = {"DATABASE_URL": schema_url, "APP_ENV": "test", "MIGRATIONS_DIR": str(MIGRATIONS)}
    p = create_pool(load_settings(env))
    p.wait(timeout=10)
    yield p
    p.close()


def attempt(pool, key, items):
    request = parse_order(json.dumps(body(items)).encode())
    try:
        return ("ok", create_order(pool, key, request).order_id)
    except ApiError as error:
        return (error.code, None)


def available(pool, sku):
    with pool.connection() as conn:
        return conn.execute("SELECT available FROM products WHERE sku = %s", (sku,)).fetchone()[
            "available"
        ]


def test_race_for_the_last_units_never_oversells(pool):
    with ThreadPoolExecutor(max_workers=17) as ex:
        results = list(
            ex.map(lambda i: attempt(pool, f"race-key-{i:04d}", [line("PART-200")]), range(17))
        )
    outcomes = [r[0] for r in results]
    assert outcomes.count("ok") == 12 and outcomes.count("OUT_OF_STOCK") == 5
    assert available(pool, "PART-200") == 0


def test_eight_identical_requests_make_one_order_and_reserve_once(pool):
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(
            ex.map(lambda _: attempt(pool, "same-key-0001", [line("PACK-300")]), range(8))
        )
    assert {r[0] for r in results} == {"ok"}
    assert len({r[1] for r in results}) == 1
    assert available(pool, "PACK-300") == 4


def test_crossed_multi_sku_orders_do_not_deadlock(pool):
    def run(i):
        items = (
            [line("KIT-100"), line("PART-200")] if i % 2 else [line("PART-200"), line("KIT-100")]
        )
        return attempt(pool, f"cross-key-{i:04d}", items)

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(run, range(10)))
    assert [r[0] for r in results].count("ok") == 7  # KIT-100 has 7 units
    assert available(pool, "KIT-100") == 0
