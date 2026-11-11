"""The FastAPI app: health, catalog, inventory, orders and /metrics.

Routes are plain `def`, so each request borrows one connection from the pool
and runs in FastAPI's thread pool. Only POST /orders is `async`, because it has
to read the raw body; it hands the database work to the thread pool.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from fulfillops.api.errors import ApiError, install_error_handlers
from fulfillops.api.metrics import Metrics
from fulfillops.api.orders import (
    MAX_BODY_BYTES,
    check_idempotency_key,
    create_order,
    get_order,
    parse_order,
)
from fulfillops.common.db import create_pool
from fulfillops.common.migrate import run_migrations, schema_is_current
from fulfillops.common.settings import Settings, load_settings

log = logging.getLogger("fulfillops.api")

MIGRATION_RETRY_SECONDS = 2.0


def _migrate_in_background(settings: Settings, stop: threading.Event) -> None:
    """Keep trying until the schema is in place, so a slow database never kills the API."""
    while not stop.is_set():
        try:
            run_migrations(settings.database_url, settings.migrations_dir)
            return
        except Exception as exc:
            log.warning("migrations not applied yet (%s), retrying", type(exc).__name__)
            stop.wait(MIGRATION_RETRY_SECONDS)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = create_pool(settings)
        stop = threading.Event()
        migrator = threading.Thread(
            target=_migrate_in_background, args=(settings, stop), daemon=True
        )
        migrator.start()
        app.state.pool = pool
        app.state.metrics = Metrics(pool)
        try:
            yield
        finally:
            stop.set()
            pool.close()

    app = FastAPI(title="FulfillOps API", lifespan=lifespan, docs_url=None, redoc_url=None)
    install_error_handlers(app)

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", "unmatched")
            request.app.state.metrics.observe(
                request.method, template, status, time.perf_counter() - started
            )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request):
        not_ready = JSONResponse({"status": "not_ready"}, status_code=503)
        if settings.fail_readiness:
            return not_ready
        try:
            with request.app.state.pool.connection(timeout=2.0) as conn:
                if not schema_is_current(conn, settings.migrations_dir):
                    return not_ready
        except Exception:
            return not_ready
        return {"status": "ready"}

    @app.get("/api/v1/catalog")
    def catalog(request: Request):
        with request.app.state.pool.connection() as conn:
            rows = conn.execute(
                "SELECT sku, unit_price_cents FROM products ORDER BY sku"
            ).fetchall()
        return {"products": rows}

    @app.get("/api/v1/inventory/{sku}")
    def inventory(sku: str, request: Request):
        with request.app.state.pool.connection() as conn:
            row = conn.execute(
                "SELECT sku, available FROM products WHERE sku = %s", (sku,)
            ).fetchone()
        if row is None:
            raise ApiError(404, "SKU_NOT_FOUND", "Unknown SKU")
        return row

    @app.post("/api/v1/orders", status_code=202)
    async def post_order(request: Request):
        key = check_idempotency_key(request.headers.get("idempotency-key"))
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise ApiError(413, "INVALID_REQUEST", "Request body is too large (limit 16 KiB)")
        order = parse_order(await request.body())
        receipt = await run_in_threadpool(create_order, request.app.state.pool, key, order)
        return {"order_id": receipt.order_id, "status": receipt.status}

    @app.get("/api/v1/orders/{order_id}")
    def read_order(order_id: str, request: Request):
        return get_order(request.app.state.pool, order_id)

    @app.get("/metrics")
    def metrics(request: Request):
        body = generate_latest(request.app.state.metrics.registry)
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    return app
