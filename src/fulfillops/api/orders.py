"""Orders: validation, the accept-an-order transaction, and reading an order back.

create_order is the heart of the system (ADR-001, D3, D5, D6). Everything that
must be all-or-nothing happens inside one database transaction, and the 202 is
only sent after it commits.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from fulfillops.api.errors import ApiError

MAX_BODY_BYTES = 16 * 1024
MAX_ITEMS = 10
MAX_QUANTITY = 20
MAX_CUSTOMER_ID = 64
_KEY_PATTERN = re.compile(r"^[\x20-\x7e]{8,128}$")  # printable ASCII, 8 to 128 characters


@dataclass(frozen=True)
class OrderRequest:
    customer_id: str
    lines: tuple[tuple[str, int], ...]  # one entry per SKU, quantities added, sorted by SKU

    def payload_hash(self) -> str:
        canonical = json.dumps(
            {"customer_id": self.customer_id, "lines": self.lines}, sort_keys=True
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Receipt:
    order_id: str
    status: str = "queued"


def check_idempotency_key(value: str | None) -> str:
    if value is None or value == "":
        raise ApiError(400, "IDEMPOTENCY_REQUIRED", "Idempotency-Key header is required")
    if not _KEY_PATTERN.match(value):
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key must be 8-128 printable characters")
    return value


def parse_order(body: bytes) -> OrderRequest:
    """Validate in the order the contract lists: size, JSON, structure, SKU, quantity."""
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, "INVALID_REQUEST", "Request body is too large (limit 16 KiB)")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "INVALID_JSON", "Request body is not valid JSON") from None

    bad = ApiError(400, "INVALID_REQUEST", "The request is not valid")
    if not isinstance(data, dict):
        raise bad
    customer_id = data.get("customer_id")
    if not isinstance(customer_id, str) or not 1 <= len(customer_id) <= MAX_CUSTOMER_ID:
        raise bad
    items = data.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise bad
    for item in items:
        if not isinstance(item, dict) or "sku" not in item or "quantity" not in item:
            raise bad

    totals: dict[str, int] = {}
    for item in items:
        sku, quantity = item["sku"], item["quantity"]
        if not isinstance(sku, str) or sku == "":
            raise ApiError(422, "INVALID_SKU", "Unknown SKU")
        # bool is a subclass of int in Python, so it needs its own check
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ApiError(422, "INVALID_QUANTITY", "Quantity must be an integer from 1 to 20")
        if not 1 <= quantity <= MAX_QUANTITY:
            raise ApiError(422, "INVALID_QUANTITY", "Quantity must be an integer from 1 to 20")
        totals[sku] = totals.get(sku, 0) + quantity
    return OrderRequest(customer_id, tuple(sorted(totals.items())))


def create_order(pool: ConnectionPool, key: str, order: OrderRequest) -> Receipt:
    skus = [sku for sku, _ in order.lines]
    with pool.connection() as conn, conn.transaction():
        prices = {
            row["sku"]: row["unit_price_cents"]
            for row in conn.execute(
                "SELECT sku, unit_price_cents FROM products WHERE sku = ANY(%s)", (skus,)
            )
        }
        if len(prices) != len(skus):
            raise ApiError(422, "INVALID_SKU", "Unknown SKU")
        total = sum(prices[sku] * qty for sku, qty in order.lines)

        # the unique key decides who owns this order, even across replicas
        order_id = str(uuid.uuid4())
        created = conn.execute(
            """
            INSERT INTO orders (id, customer_id, idempotency_key, payload_hash, total_cents)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (order_id, order.customer_id, key, order.payload_hash(), total),
        ).fetchone()

        if created is None:  # somebody already used this key
            existing = conn.execute(
                "SELECT id, payload_hash FROM orders WHERE idempotency_key = %s", (key,)
            ).fetchone()
            if existing["payload_hash"] != order.payload_hash():
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Key was used with a different request")
            return Receipt(existing["id"])

        # SKUs are sorted, so concurrent orders lock rows in the same order: no deadlocks
        for sku, qty in order.lines:
            reserved = conn.execute(
                "UPDATE products SET available = available - %s WHERE sku = %s AND available >= %s",
                (qty, sku, qty),
            )
            if reserved.rowcount == 0:
                # raising rolls everything back, including the order row and earlier SKUs
                raise ApiError(409, "OUT_OF_STOCK", "Not enough items available")
        for sku, qty in order.lines:
            conn.execute(
                "INSERT INTO order_items (order_id, sku, quantity) VALUES (%s, %s, %s)",
                (order_id, sku, qty),
            )
        conn.execute("INSERT INTO jobs (order_id) VALUES (%s)", (order_id,))
    return Receipt(order_id)  # reached only after COMMIT


def get_order(pool: ConnectionPool, order_id: str) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id AS order_id, status, total_cents FROM orders WHERE id = %s", (order_id,)
        ).fetchone()
    if row is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    return row
