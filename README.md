# FulfillOps

A resilient order-processing platform, built as the EPITA DevOps capstone by a team of three.
Customers place orders through an HTTP API. The API reserves stock and answers straight away; a
separate worker finishes the order in the background. Nothing is lost if a worker crashes, a request
is retried, or two customers want the last unit.

- **API** (`src/fulfillops/api`): FastAPI. Accepts orders, reserves stock, serves inventory and metrics.
- **Worker** (`src/fulfillops/worker`): takes jobs from a queue stored in PostgreSQL and completes orders.
- **Shared code** (`src/fulfillops/common`): settings, database pool, migrations.
- **Database**: PostgreSQL 16. Plain SQL migrations in `db/migrations`.

One package and one image, started with two different commands:
`python -m fulfillops.api` and `python -m fulfillops.worker`.

The reasons behind each design choice, the alternatives we rejected and the known limitations are in
[`docs/ADR-001.md`](docs/ADR-001.md). Read it before the code.

## How an order flows

1. `POST /api/v1/orders` with an `Idempotency-Key` header.
2. In **one database transaction** the API stores the order and its lines, reduces the stock of every
   SKU with a conditional update (it only succeeds if enough stock is left), and creates a job.
   If any SKU is short, everything is rolled back and the answer is `409 OUT_OF_STOCK`.
3. The API answers `202` with the order id and status `queued`.
4. A worker claims the job (`FOR UPDATE SKIP LOCKED`, 10 s lease), does the work and marks the order
   `completed`. If the worker dies, the lease expires and another worker takes over.
5. A job that keeps failing is retried with backoff (2, 4, 8, 16 s, about 25 % jitter, 30 s cap).
   After 5 attempts the job is `dead` and the order is `failed`. Its stock is not given back
   automatically (see ADR-001, section 7).

Sending the same request again with the same key returns the same order and reserves stock once.
The same key with a different body returns `409 IDEMPOTENCY_CONFLICT`.

## API

| Method and path | What it does |
|---|---|
| `GET /health/live` | Process is up. Does not touch the database. |
| `GET /health/ready` | `200` when the database is reachable and migrations are applied, otherwise `503`. |
| `GET /api/v1/catalog` | The three products and their fixed prices. |
| `GET /api/v1/inventory/{sku}` | Available stock for one SKU. |
| `POST /api/v1/orders` | Create an order. Requires `Idempotency-Key`. Returns `202`. |
| `GET /api/v1/orders/{order_id}` | Status (`queued`, `processing`, `completed`, `failed`) and total. |
| `GET /metrics` | Prometheus metrics (request counts and latency, orders created and failed, worker retries). |

Request body for `POST /api/v1/orders`:

```json
{ "customer_id": "CUST-1", "items": [ { "sku": "PART-200", "quantity": 2 } ] }
```

Errors always use the same shape, `{"error": {"code": "...", "message": "..."}}`, and never contain
SQL or stack traces. Validation runs in this order:

| Check | Answer |
|---|---|
| `Idempotency-Key` missing | 400 `IDEMPOTENCY_REQUIRED` |
| `Idempotency-Key` malformed (not 8 to 128 printable characters) | 400 `INVALID_REQUEST` |
| Body over 16 KiB | 413 `INVALID_REQUEST` (our decision, see ADR-001 section 6) |
| Body is not JSON | 400 `INVALID_JSON` |
| Wrong structure (customer id, 1 to 10 items) | 400 `INVALID_REQUEST` |
| Empty or unknown SKU | 422 `INVALID_SKU` |
| Quantity not a whole number from 1 to 20 | 422 `INVALID_QUANTITY` |
| Not enough stock | 409 `OUT_OF_STOCK` |
| Same key, different body | 409 `IDEMPOTENCY_CONFLICT` |
| Unknown SKU or order on a read | 404 `SKU_NOT_FOUND` / `ORDER_NOT_FOUND` |

## Run it locally

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and Docker (only for the database).

```bash
# 1. a throwaway PostgreSQL for development and tests
docker run --name fo-test-db -e POSTGRES_PASSWORD=test -e POSTGRES_DB=fulfillops_test \
  -p 55432:5432 -d postgres:16

# 2. install dependencies from the lockfile
uv sync

# 3. start the API first: it applies the migrations and seeds the catalog
export DATABASE_URL='postgresql://postgres:test@localhost:55432/fulfillops_test'
export APP_ENV=development
uv run python -m fulfillops.api

# 4. in a second terminal (same two exports), start a worker
uv run python -m fulfillops.worker
```

Start the API before the worker. A worker that starts first logs "relation does not exist" until the
migrations have run; it recovers on its own.

All settings are environment variables with defaults, listed in [`.env.example`](.env.example).
Only `DATABASE_URL` is required. `APP_ENV` defaults to `production`; use `development` or `test`
locally. Never put real secrets in Git: `.env.example` holds placeholders only.

## Tests

```bash
export DATABASE_URL='postgresql://postgres:test@localhost:55432/fulfillops_test'
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

Expect every test to pass with **0 skipped**. (SIGTERM tests skip on Windows only.) Without `DATABASE_URL` the database tests are skipped and the run
only looks green, so make sure it is set (CI included). Each test works in its own temporary schema,
so the suite does not disturb data in the test database.

What the tests cover:

- `tests/test_orders.py`: validation order, idempotency, out of stock, and the stock race (many
  concurrent orders for few units never oversell).
- `tests/test_worker.py`: claiming, leases, guarded completion, retries, dead jobs, two workers,
  SIGTERM on the worker.
- `tests/test_shutdown.py`: SIGTERM on the API lets a request that is already running finish.
- `tests/test_integration.py`: the real API and real worker processes together, including a worker
  killed with SIGKILL mid-job and a poison order.
- `tests/test_api_basics.py`, `test_migrate.py`, `test_settings.py`: health, metrics, migrations, config.

## The public checker

The course checker runs against a running API and worker (run it only against a test database,
because its orders consume stock):

```bash
python3 fulfillops_public_checker.py --base-url http://localhost:8080 --concurrency --stress
```

Our result on a freshly reset database is **15 passed, 0 failed, 0 skipped**. Each run uses up some
of the seeded stock, so on repeated runs the contention and stress checks may skip. Reset the
test data before running it again: `APP_ENV=test python scripts/reset_test_data.py --yes` (stop the
workers first). The script refuses to run unless `APP_ENV=test`.

## Demos

**The stock race, with and without the fix.** Applies a deliberately naive version (read the stock,
check it in Python, write it back), runs the race test so you can watch it oversell, then restores
the correct code. Needs `DATABASE_URL` pointing at the test database and a clean `orders.py`:

```bash
bash scripts/demo-race-bug.sh
```

**Worker crash and poison job.**

```bash
bash scripts/drill-worker-kill-local.sh   # kill a worker mid-job, another one finishes it
bash scripts/drill-poison-local.sh        # an order that always fails ends as failed after 5 attempts
```

`POISON_CUSTOMER_ID` makes the worker fail every order from that customer. It is ignored when
`APP_ENV=production`, and the API has no special route for it.

**Postman.** Import `docs/postman/FulfillOps.postman_collection.json` (18 requests with assertions).
Screenshots of a full run are in `docs/postman/run_photos/`.

## Operations notes

- **Shutdown.** On SIGTERM the API stops accepting connections, lets running requests finish (20 s
  cap) and exits. The worker finishes its current job, then stops. In Kubernetes give both
  `terminationGracePeriodSeconds: 30` and a short `preStop` sleep.
- **Metrics.** `orders_created`, `orders_failed` and `worker_retries` are read from the database when
  Prometheus scrapes the API, so the worker needs no metrics port. With several API replicas read
  those three with `max`; use `sum` or `rate` for the request series.
- **Readiness.** `FAIL_READINESS=true` makes `/health/ready` return 503. It exists to simulate a
  broken release in the deployment demo; liveness stays 200.

## Docker, CI and Kubernetes

Owned by Tony. This section is a placeholder until their work is merged: image build, Docker
Compose, the CI workflow, the Kubernetes manifests in `infra/k8s` and the Argo CD application in
`infra/argocd`, plus the cluster race proof.

## Documentation

- [`docs/ADR-001.md`](docs/ADR-001.md): architecture decisions, data flow, alternatives, limitations.
- `docs/postman/`: API collection and run screenshots.
- `docs/demo/`: the deliberate-bug patch used by the race demo.

## Team

Three people, three areas: the API and stock reservation (Makuo), the worker and reliability
(Saad), and Docker, CI and Kubernetes with Argo CD (Tony).

## Individual notes

Each person adds their own subsection here: what they implemented, what they reviewed, and what
they would improve next. AI assistance is listed per person in [`docs/assistance-log.md`](docs/assistance-log.md).

### Makuo

**What I implemented.**
- The API and the order transaction in `src/fulfillops/api/`: validation with exact status and error codes, atomic stock reservation, and idempotency across replicas with a unique key and a payload hash.
- The schema and migrations in `db/migrations/`, and ADR-001 (decisions, invariants, limitations).
- My own tests: unit and API tests, the two race tests (different keys against identical keys), the integration tests with real worker processes (`tests/test_integration.py`), and the deliberate-bug demo that goes red and then green.
- Graceful shutdown on SIGTERM, the 413 decision for bodies over 16 KiB, the guarded reset script `scripts/reset_test_data.py`, and the Postman collection.
- The README, `docs/architecture.md`, and the cluster race-proof script `scripts/cluster-race-proof.sh`.

**What I reviewed.** Saad's worker, drill and documentation pull requests (#9, #10, #11, #13, #17), and Tony's Docker, Kubernetes, Argo CD and rollout pull requests (#16, #19, #22). On Tony's work I asked for the explicit rolling-update strategy, `Recreate` for PostgreSQL, Argo CD tracking `main`, and fixes to the race script (waiting only for the POST processes, asserting results, keeping the executable bit).

**What I would improve next.**
- Idempotency keys never expire: add a retention window with a cleanup job.
- A rejected order (409) frees its key, so a retry can succeed later: store the rejected result if strict replay is needed.
- No authentication or rate limiting: add API keys or OAuth and a gateway limit.
- A `failed` order keeps its stock reserved: add an operator action with an audit trail to retry or release it.
- Stock safety depends on every code path using the conditional update: move every stock change into one function covered by the race tests.
