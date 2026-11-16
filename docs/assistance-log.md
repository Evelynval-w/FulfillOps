# AI assistance log

Rule for this file: one row per real piece of work (code, tests, docs, configuration), not per chat
question. Each person adds their own rows and does not edit the rows of others. The last column
says what the person did or checked personally; it must be true.

| Date | Person | Tool | What the AI helped with | What I decided, did and checked myself |
|------|--------|------|-------------------------|-----------------------------------------|
| 2026-11-11 | Saad (B) | Claude | Drafted the worker: claim with `SKIP LOCKED`, lease, guarded completion, retries with backoff, dead letter, SIGTERM stop (`src/fulfillops/worker/`) | Chose the design direction with Maku (jobs table is the queue, metrics from the database, no worker `/metrics` port, poison job by environment variable). Ran the 14 worker tests on my Windows machine against PostgreSQL in Docker. I can explain each function in `queue.py` and `runner.py`. |
| 2026-11-11 | Saad (B) | Claude | Wrote `tests/test_worker.py` (lost lease, crash recovery, poison job, two workers, SIGTERM) | Ran them locally; the same scenarios are also covered end to end by Maku's `tests/test_integration.py` |
| 2026-11-12 | Saad (B) | Claude | Worker startup fix (log once, back off, recover) and the `_int` to `int_setting` rename | Checked the diff (4 files), ran the worker tests, opened the pull request |
| 2026-11-12 | Saad (B) | Claude | Drafted `docs/ADR-003.md` and `docs/threat-model.md` | Read both against the worker code and the contract and corrected what I could not defend. Opened the pull request (#9) for review. |
| 2026-11-12 | Saad (B) | Claude | Wrote `scripts/measure_p95.py` (200 samples per endpoint, p95, completion time) | Ran it on my machine (1 API, 2 workers), checked the numbers against the server histogram, committed the evidence files |
| 2026-11-12 | Saad (B) | Claude | Wrote the worker "break it on purpose" demo (`scripts/demo-worker-bug.*`) | Ran it: the guard test went red with the planted bug and green after the restore |
| 2026-11-12 | Saad (B) | Claude | Explained git, Docker and PowerShell commands | Ran every command myself, fixed my own environment (stopped container, missing `DATABASE_URL`) |
| 2026-11-09 | Makuo (A) | Claude | Rewrote `docs/ADR-001.md` and drafted the schema and migrations (orders, items, products, jobs) | Chose the rules myself with the team: one transaction, conditional stock update, idempotency key, queue in PostgreSQL. Read the SQL and can explain each constraint. Ran the migration on my PostgreSQL in Docker. |
| 2026-11-10 | Makuo (A) | Claude | Wrote the API: `POST /orders`, `GET /orders/{id}`, validation, error codes, health and metrics endpoints (`src/fulfillops/api/`) | Checked the error codes and validation order against the brief. Ran the app and the public checker on my Mac. |
| 2026-11-10 | Makuo (A) | Claude | Wrote `tests/test_orders.py` and the race and idempotency tests (concurrent orders, replay, conflict) | Ran the suite on my Mac against PostgreSQL in Docker. I can explain why the stock cannot go negative. |
| 2026-11-10 | Makuo (A) | Claude | Built the Postman collection and the "break it on purpose" race demo (`docs/demo/`, demo script) | Ran the collection in Postman. Ran the demo: the race test went red with the planted bug and green after the restore. |
| 2026-11-11 | Makuo (A) | Claude | SIGTERM graceful shutdown (`timeout_graceful_shutdown=20`) and `tests/test_shutdown.py` | Ran the tests. Decided the Windows skip with Saad after his test failed on Windows. |
| 2026-11-12 | Makuo (A) | Claude | The 413 decision for bodies over 16 KiB, the code change, the tests and the note in ADR-001 | Made the decision myself (413 over 400) and asked for it in the ADR. Re-ran the suite. |
| 2026-11-12 | Makuo (A) | Claude | `tests/stack.py`, `tests/test_integration.py` (worker kill, poison job, readiness) and `scripts/reset_test_data.py` with its test | Ran the full suite on my Mac and the public checker (15 passed, 0 failed, 0 skipped). Chose the guard: the script refuses unless `APP_ENV=test` and `--yes`. |
| 2026-11-12 | Makuo (A) | Claude | Drafted the README, the playbook and the defense notes | Read them against the code and corrected what was wrong. Rehearsed the answers aloud. |
| 2026-11-12 | Makuo (A) | Claude | Reviewed Saad's pull requests #9, #10 and #11 and drafted the review comments | Read the diffs, ran the tests, then approved and merged them myself. |
| 2026-11-13 | Saad (B) | Claude | Wrote the worker-kill and poison-job drill commands for Docker Compose and the incident postmortem text (`docs/incident-postmortem.md`) | Ran both drills myself on the Compose stack, saved the logs, metrics, SQL checks and screenshots in `docs/evidence/`, and checked every number in the postmortem against those files |
| 2026-11-13 | Saad (B) | Claude | Wrote `scripts/drill-k8s-kill.sh` (worker kill drill for the Kubernetes cluster) | Committed it before the code freeze. It was not run before the freeze, so the cluster drill is reported as planned |
| 2026-11-14 | Saad (B) | Claude | Wrote my live demo helper script and speaking notes for the presentation (kept outside the repository) | Rehearsed the three demos on my machine and fixed a problem found in rehearsal (a leftover `DATABASE_URL` broke the Compose stack) |
| 2026-11-15 | Saad (B) | Claude | Drafted my section of the report (worker, reliability and observability) | Checked every number against `docs/evidence/` (p95 JSON, drill logs, integrity files) and kept the cluster drill marked as planned |

Not AI-assisted: team decisions, group messages and review comments.


