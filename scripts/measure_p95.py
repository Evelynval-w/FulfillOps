#!/usr/bin/env python3
"""Measure request latency (p95) and order completion time for FulfillOps.

TEST DEPLOYMENTS ONLY. This creates orders and uses up stock. Never run it against production.

Standard library only, so it runs anywhere Python 3.11+ runs:

    python scripts/measure_p95.py --base-url http://127.0.0.1:8080 --label compose-2api-2worker

What it does (200 samples per endpoint by default):
  1. warm-up requests (not counted)
  2. POST /api/v1/orders          200 new orders, one at a time (needs 200 units of PART-200)
  3. GET  /api/v1/orders/{id}     200 reads of those orders
  4. POST /api/v1/orders          200 idempotent replays (same key and body)
  5. GET  /api/v1/catalog         200
  6. GET  /api/v1/inventory/{sku} 200
  7. wait for the queue to drain, then completion time: 20 orders sent every 0.5 s, each timed
     until its status is completed (paced below the workers' capacity, so it is not a backlog test)

p95 is the exact sample percentile (nearest rank) of the client-side timings. As a cross-check
it also estimates p95 from the server's own histogram (the way Prometheus histogram_quantile
does), using the difference of /metrics before and after the order phase.

The seed stock is only 24 units, so the order phases need more stock first. On the ISOLATED TEST
database only, run once (adjust the connection to your setup):

    UPDATE products SET available = 100000 WHERE sku = 'PART-200';

Raw samples and a summary are written to docs/evidence/ so the run is reproducible and can be
cited in the report.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import platform
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

SKU = "PART-200"
OK_STATUSES = range(200, 300)


class Client:
    """One keep-alive connection, so connection set-up time is not part of every sample."""

    def __init__(self, base_url: str):
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=30)

    def request(self, method: str, path: str, body: dict | None = None, headers=None):
        payload = json.dumps(body).encode() if body is not None else None
        hdrs = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        start = time.perf_counter()
        try:
            self.conn.request(method, path, body=payload, headers=hdrs)
            resp = self.conn.getresponse()
            data = resp.read()
        except (http.client.HTTPException, OSError):
            self.conn.close()  # reconnect on the next call
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
            raise
        elapsed = time.perf_counter() - start
        return resp.status, data, elapsed


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least q of the samples at or below it."""
    if not sorted_values:
        return float("nan")
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(samples: list[tuple[int, float]]) -> dict:
    ms = sorted(t * 1000 for _, t in samples)
    ok = sum(1 for s, _ in samples if s in OK_STATUSES)
    return {
        "samples": len(samples),
        "success": ok,
        "non_2xx": len(samples) - ok,
        "min_ms": round(ms[0], 2),
        "mean_ms": round(statistics.fmean(ms), 2),
        "p50_ms": round(percentile(ms, 0.50), 2),
        "p95_ms": round(percentile(ms, 0.95), 2),
        "p99_ms": round(percentile(ms, 0.99), 2),
        "max_ms": round(ms[-1], 2),
    }


# ---------- server histogram cross-check ----------


def parse_buckets(metrics_text: str, method: str, route: str) -> dict[float, float]:
    """Cumulative histogram buckets of fulfillops_http_request_duration_seconds for one route."""
    prefix = "fulfillops_http_request_duration_seconds_bucket{"
    buckets: dict[float, float] = {}
    for line in metrics_text.splitlines():
        if not line.startswith(prefix):
            continue
        labels, _, value = line[len(prefix) :].rpartition("} ")
        if f'method="{method}"' not in labels or f'route="{route}"' not in labels:
            continue
        le = labels.split('le="', 1)[1].split('"', 1)[0]
        buckets[math.inf if le == "+Inf" else float(le)] = float(value)
    return buckets


def histogram_quantile(q: float, before: dict[float, float], after: dict[float, float]):
    """Linear interpolation inside the bucket, like Prometheus histogram_quantile."""
    delta = sorted((le, after.get(le, 0) - before.get(le, 0)) for le in after)
    if not delta or delta[-1][1] <= 0:
        return None
    target = q * delta[-1][1]
    lower_le, lower_count = 0.0, 0.0
    for le, count in delta:
        if count >= target:
            if le == math.inf:
                return lower_le  # the quantile is above the last finite bucket
            if count == lower_count:
                return le
            return lower_le + (le - lower_le) * (target - lower_count) / (count - lower_count)
        lower_le, lower_count = le, count
    return None


# ---------- phases ----------


def order_body(customer: str) -> dict:
    return {"customer_id": customer, "items": [{"sku": SKU, "quantity": 1}]}


def stock_hint() -> str:
    return (
        "Out of stock. On the ISOLATED TEST database only, raise the stock first:\n"
        f"    UPDATE products SET available = 100000 WHERE sku = '{SKU}';"
    )


def timed(client: Client, method, path, body=None, headers=None):
    status, data, elapsed = client.request(method, path, body, headers)
    return status, data, elapsed


def run(args) -> int:
    client = Client(args.base_url)
    run_id = uuid.uuid4().hex[:8]
    n = args.samples
    raw: list[dict] = []  # every counted sample, for the CSV
    results: dict[str, dict] = {}

    def record(name: str, samples: list[tuple[int, float]]):
        for status, elapsed in samples:
            raw.append({"endpoint": name, "status": status, "ms": round(elapsed * 1000, 3)})
        results[name] = summarize(samples)

    # sanity: the service is up
    status, _, _ = client.request("GET", "/health/ready")
    if status != 200:
        print(f"API is not ready (status {status}). Start the stack first.", file=sys.stderr)
        return 2

    print(f"warm-up ({args.warmup} requests, not counted)...")
    for _ in range(args.warmup):
        client.request("GET", "/api/v1/catalog")

    # 2. create orders (also the cross-check source)
    _, before, _ = client.request("GET", "/metrics")
    print(f"POST /api/v1/orders x {n} (new orders)...")
    created: list[tuple[str, str, dict]] = []  # (order_id, idempotency key, body)
    create_samples: list[tuple[int, float]] = []
    for i in range(n):
        key = f"p95-{run_id}-{i:05d}"
        body = order_body(f"CUST-P95-{i % 50}")
        status, data, elapsed = timed(
            client, "POST", "/api/v1/orders", body, {"Idempotency-Key": key}
        )
        if status == 409:
            print(stock_hint(), file=sys.stderr)
            return 3
        create_samples.append((status, elapsed))
        if status in OK_STATUSES:
            created.append((json.loads(data)["order_id"], key, body))
    record("POST /orders (new order)", create_samples)
    _, after, _ = client.request("GET", "/metrics")

    server_p95 = histogram_quantile(
        0.95,
        parse_buckets(before.decode(), "POST", "/api/v1/orders"),
        parse_buckets(after.decode(), "POST", "/api/v1/orders"),
    )

    if not created:
        print("No order was created, cannot continue.", file=sys.stderr)
        return 3

    # 3. read orders
    print(f"GET /api/v1/orders/{{id}} x {n}...")
    samples = []
    for i in range(n):
        order_id = created[i % len(created)][0]
        status, _, elapsed = timed(client, "GET", f"/api/v1/orders/{order_id}")
        samples.append((status, elapsed))
    record("GET /orders/{id}", samples)

    # 4. idempotent replays
    print(f"POST /api/v1/orders x {n} (idempotent replays)...")
    samples = []
    for i in range(n):
        _, key, body = created[i % len(created)]
        status, _, elapsed = timed(client, "POST", "/api/v1/orders", body, {"Idempotency-Key": key})
        samples.append((status, elapsed))
    record("POST /orders (replay)", samples)

    # 5. catalog, 6. inventory
    print(f"GET /api/v1/catalog x {n}...")
    samples = [(s, e) for s, _, e in (timed(client, "GET", "/api/v1/catalog") for _ in range(n))]
    record("GET /catalog", samples)
    print(f"GET /api/v1/inventory/{SKU} x {n}...")
    samples = [
        (s, e) for s, _, e in (timed(client, "GET", f"/api/v1/inventory/{SKU}") for _ in range(n))
    ]
    record("GET /inventory/{sku}", samples)

    # 7. completion time, paced below the workers' capacity
    completion = None
    if args.completion > 0:
        # the 200 orders above are still in the queue; timing a new order behind them would
        # measure that backlog, not the normal completion time
        print("waiting for the queue to drain before timing completion...")
        last_id = created[-1][0]
        drain_start = time.perf_counter()
        while time.perf_counter() - drain_start < args.drain_timeout:
            _, data, _ = timed(client, "GET", f"/api/v1/orders/{last_id}")
            if json.loads(data).get("status") == "completed":
                break
            time.sleep(0.5)
        else:
            print("The queue did not drain in time: is a worker running?", file=sys.stderr)
            return 4
        print(f"  queue drained after {time.perf_counter() - drain_start:.0f} s")
        print(f"completion time: {args.completion} orders, one every {args.pace} s...")
        # send one order every `pace` seconds and poll the pending ones in between, so the time
        # of each order is measured while it runs (not after all of them were sent)
        pending: dict[str, float] = {}  # order id -> send time
        times: list[float] = []
        sent_count = 0
        timeouts = 0

        def poll_pending() -> None:
            nonlocal timeouts
            for order_id, t0 in list(pending.items()):
                _, data, _ = timed(client, "GET", f"/api/v1/orders/{order_id}")
                now = time.perf_counter()
                if json.loads(data).get("status") == "completed":
                    times.append(now - t0)
                    del pending[order_id]
                elif now - t0 > args.completion_timeout:
                    timeouts += 1
                    del pending[order_id]

        for i in range(args.completion):
            key = f"p95-c-{run_id}-{i:05d}"
            t0 = time.perf_counter()
            status, data, _ = timed(
                client, "POST", "/api/v1/orders", order_body("CUST-P95-C"), {"Idempotency-Key": key}
            )
            if status == 409:
                print(stock_hint(), file=sys.stderr)
                return 3
            if status in OK_STATUSES:
                pending[json.loads(data)["order_id"]] = t0
                sent_count += 1
            while time.perf_counter() - t0 < args.pace:
                poll_pending()
                time.sleep(0.05)
        while pending:
            poll_pending()
            time.sleep(0.05)
        sent = range(sent_count)
        ms = sorted(t * 1000 for t in times)
        within = sum(1 for t in times if t <= 15.0)
        completion = {
            "orders": len(sent),
            "completed": len(times),
            "timeouts": timeouts,
            "within_15s": within,
            "p50_ms": round(percentile(ms, 0.5), 1) if ms else None,
            "p95_ms": round(percentile(ms, 0.95), 1) if ms else None,
            "max_ms": round(ms[-1], 1) if ms else None,
        }

    # ---------- report ----------
    target = args.target_ms
    print()
    header = f"{'endpoint':<26}{'n':>5}{'non2xx':>8}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}"
    print(f"{header}   vs {target:g} ms")
    for name, r in results.items():
        verdict = "OK " if r["p95_ms"] < target else "OVER"
        print(
            f"{name:<26}{r['samples']:>5}{r['non_2xx']:>8}{r['p50_ms']:>9}{r['p95_ms']:>9}"
            f"{r['p99_ms']:>9}{r['max_ms']:>9}   {verdict}"
        )
    if server_p95 is not None:
        print(f"\nserver histogram p95 for POST /orders (interpolated): {server_p95 * 1000:.1f} ms")
        print("  (coarse: limited by bucket edges; excludes network and client time)")
    if completion:
        print(f"\ncompletion: {completion}")

    summary = {
        "date_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "label": args.label,
        "base_url": args.base_url,
        "samples_per_endpoint": n,
        "target_p95_ms": target,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": _git_commit(),
        "method": "client-side wall time, one keep-alive connection, sequential requests, "
        "nearest-rank percentile",
        "limitations": [
            "single machine: client, API, workers and database share CPU and memory",
            "one sequential client: no concurrent load, queueing under load is not measured",
            "simulated worker processing time, no real external system",
            "seed data of a few rows: query times do not reflect production data volumes",
        ],
        "endpoints": results,
        "server_histogram_p95_ms_post_orders": None
        if server_p95 is None
        else round(server_p95 * 1000, 1),
        "completion": completion,
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"p95-{stamp}.json"
    csv_path = out_dir / f"p95-{stamp}-samples.csv"
    json_path.write_text(json.dumps(summary, indent=2) + "\n")
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["endpoint", "status", "ms"])
        writer.writeheader()
        writer.writerows(raw)
    print(f"\nwrote {json_path} and {csv_path}")
    incomplete = [k for k, r in results.items() if r["samples"] < n]
    return 1 if incomplete else 0


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    p.add_argument(
        "--samples", type=int, default=200, help="samples per endpoint (brief: at least 200)"
    )
    p.add_argument("--warmup", type=int, default=20)
    p.add_argument(
        "--completion", type=int, default=20, help="orders for the completion time test (0 = skip)"
    )
    p.add_argument("--pace", type=float, default=0.5, help="seconds between completion-test orders")
    p.add_argument("--completion-timeout", type=float, default=30.0)
    p.add_argument(
        "--drain-timeout", type=float, default=300.0, help="max wait for the queue to empty"
    )
    p.add_argument(
        "--target-ms", type=float, default=300.0, help="p95 target for the verdict column"
    )
    p.add_argument(
        "--label", default="local", help="short description of the setup, saved in the output"
    )
    p.add_argument("--out-dir", default="docs/evidence")
    args = p.parse_args()
    if args.samples < 200:
        print("warning: the brief asks for at least 200 samples", file=sys.stderr)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
