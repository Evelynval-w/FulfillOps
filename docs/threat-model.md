# Threat model: FulfillOps

- **Owner:** Saad (Tony adds the CI, container and Kubernetes rows once they exist)
- **Date:** 2026-11-11
- **Method:** We list what an attacker or a mistake could do to each part of the system, what we do
  about it today, and what is still open. Status is `Done` (in the code or tests), `Planned` (someone
  owns it and it is not merged yet) or `Accepted` (we know and do not fix it in this project).

## 1. What we protect

- Stock counts and orders (integrity: no overselling, no duplicates, no lost orders).
- The database password and any tokens (confidentiality).
- Availability of the API and the worker.
- The software supply chain (the image we build and deploy).

## 2. Where the trust boundaries are

```
Client --> API (2 replicas) --> PostgreSQL <-- Worker (2 replicas)
              ^                     ^
        Prometheus (/metrics)   Kubernetes Secret, Git, CI, container registry
```

The client is not trusted. The API, the worker and PostgreSQL trust each other inside the cluster.

## 3. Threats

| # | Threat | What could happen | What we do | Status |
|---|--------|-------------------|------------|--------|
| T1 | SQL injection through order fields | An attacker reads or changes data through `customer_id` or `sku` | Every query uses bound parameters (psycopg `%s`), never string building. Input is validated first (types, lengths, quantity 1 to 20) | Done |
| T2 | Replay or double submit of an order | The same order is created twice, stock is taken twice | Unique idempotency key in the database, payload hash, same key with another body gives 409 | Done |
| T3 | Overselling through a race | Many parallel requests drive stock below 0 | One conditional `UPDATE ... WHERE available >= qty` per SKU, plus `CHECK (available >= 0)` | Done |
| T4 | Oversized or malformed body | Memory pressure, parser errors, crashes | Body limit of 16 KiB (413), strict validation, one error format that never shows internals | Done |
| T5 | A test-only switch used in production | `POISON_CUSTOMER_ID` or `FAIL_READINESS` lets someone break orders or readiness | The poison id is ignored when `APP_ENV=production` (default is `production`). `FAIL_READINESS` is only set in the manifest for the demo | Done (poison); Planned (check that production manifests never set `FAIL_READINESS`) |
| T6 | Test reset script run against real data | Stock reset and orders deleted in production | The reset script refuses to run unless `APP_ENV=test` | Done (Makuo): `scripts/reset_test_data.py` |
| T7 | Secrets in Git or logs | The database password leaks | `.env` is in `.gitignore`, only placeholders in `.env.example`, the Secret holds the real value, the code never logs the database URL | Done (code); Planned (secret scan in CI, no real Secret in Git) |
| T8 | Vulnerable dependencies or image | A known flaw in a library reaches production | Locked versions in `uv.lock`; `pip-audit` or Trivy with a blocking policy; SBOM from CI | Planned (Tony) |
| T9 | Container runs as root, or CI token too powerful | A bug in the app becomes control of the host or the repository | Non-root user in the Dockerfile; minimal `permissions:` in GitHub Actions; no cluster credentials in Git | Planned (Tony) |
| T10 | Anyone who reaches the API can drain stock or flood it | Stock is bought out, or the pool is exhausted | None in the application: there is no authentication, rate limiting or per-customer quota (the contract defines none) | Accepted (documented in ADR-001 limitations) |
| T11 | `/metrics` is open | Operational data is visible to anyone on the network | None inside the application. Not exposed outside the cluster; a NetworkPolicy or authentication would be the next step | Accepted |
| T12 | A worker crashes or hangs mid-job | An order is lost, stuck, or completed twice | Lease and reclaim, fenced completion, reaper for exhausted jobs, bounded retries, SIGTERM finishes the current job | Done (tests and kill drill) |
| T13 | Poison job retries forever | A job that always fails loads the database and never ends | Five attempts, backoff, then `dead` and `failed` with `last_error`; stock not released | Done (tests and poison drill) |
| T14 | Database outage | Orders cannot be accepted or completed | `/health/ready` returns 503 so Kubernetes stops sending traffic; the worker logs the error and retries; nothing is lost because all state is in Postgres. One instance is a known single point of failure | Done (behaviour); Accepted (single instance) |
| T15 | Wrong or malicious change reaches the cluster through Git | A broken revision goes live | Pull requests with review, Argo CD shows drift, readiness probe halts a bad rollout, Git revert restores the last good one | Planned (Tony) |

## 4. What we accept in this project

Authentication, rate limiting, TLS between pods, a replicated database and encryption at rest are out
of scope for a one-week lab. We say so in the report instead of pretending they exist.

## 5. How we will show it

- Tests: `tests/test_orders.py`, `tests/test_worker.py`, `tests/test_integration.py`.
- Drill logs: worker kill and poison job (saved under `docs/evidence/`).
- CI: scan log, SBOM artifact, workflow permissions (Tony).
- A short walk-through of this table in the oral defense, picking three rows and showing the code.
