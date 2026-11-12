# Incident postmortem (blameless)

Status: DRAFT. Fill the brackets from the real drill evidence. Do not write a number or a time
that is not in a log, a metric or a query result you saved.

A postmortem explains what happened and how the system and the team can do better. It does not
look for a person to blame. Both incidents below are controlled failures, injected on purpose in
our isolated test environment.

## Incident 1: a worker is killed while processing a committed order (owner: Person B)

**Summary.** [One or two sentences: which worker was killed with which signal, which order it was
processing, how the order ended.]

**Impact.** [Orders delayed: how many and for how long. Orders lost: expected 0. Stock wrong:
expected no.]

**Timeline (UTC, from the logs).**
| Time | Event | Source |
|------|-------|--------|
| [hh:mm:ss] | Order [id] accepted, 202 returned | API log |
| [hh:mm:ss] | worker [w1] claimed job, attempt 1 | worker log |
| [hh:mm:ss] | worker [w1] killed (`docker kill` / `kubectl delete pod`) | drill command |
| [hh:mm:ss] | lease expired | `jobs.lease_expires_at` |
| [hh:mm:ss] | worker [w2] claimed job, attempt 2 | worker log |
| [hh:mm:ss] | order completed | worker log, `GET /orders/{id}` |

**Detection.** [How we could see it: `fulfillops_worker_retries_total` went up by 1, an order stayed
in `processing` for longer than usual, the log line "claimed ... attempt=2/5".]

**Root cause.** [A worker process was terminated while it held a lease. By design the job is
claimed again after the lease (10 s) expires.]

**Contributing factors.** [Fixed 10 s lease without renewal; polling every 0.5 s; processing time
raised to [5] s for the drill so the kill had a window.]

**Recovery.** [Automatic, no manual step. Time from kill to completion: [x] s, expected about the
lease time plus the polling interval.]

**Data integrity (run the queries below before, during and after).**
- Committed orders before the kill: [n]. After recovery: [n]. Lost: [0].
- The killed order completed exactly once; its job shows `attempts = 2`.
- Stock after = seed minus the quantities of all orders (no double reservation).

**What went well.** [Order not lost; no human action; logs show who held the job and when.]

**What went badly or was lucky.** [Be honest. For example: recovery time is bounded by the lease;
no alert fired; the retry metric also counts crash recoveries.]

**Action items.**
| Action | Owner | Priority |
|--------|-------|----------|
| Renew the lease while the worker is alive (heartbeat) | [B] | later |
| Alert when `fulfillops_orders_failed_total` increases | [B/C] | later |
| [your own item from the drill] | [ ] | [ ] |

### Incident 1b: poison job (same drill session)

**Summary.** [Customer id `[POISON...]` always fails. The order was tried 5 times with waits of about
2, 4, 8 and 16 s and ended `failed`; stock stayed reserved; other orders were not blocked.]
**Evidence.** [Attempt count 5, `last_error`, order status `failed`, `fulfillops_orders_failed_total`
before and after, logs with the five attempts.]

## Incident 2: failed API rolling release (owner: Person C, with Person A)

**Summary.** [A revision with `FAIL_READINESS=true` was pushed to Git; Argo CD synced it; the new
pods never became ready; the rollout stopped; the old pods kept serving.]
**Timeline (UTC).** [Commit of the bad revision, Argo CD sync, new pods not ready, probe CSV showing
the old pods answering, `git revert`, Argo CD sync, new healthy revision.]
**Impact.** [Requests failed during the rollout: from the 30 s probe CSV, expected 0.]
**Root cause.** [A deliberately unhealthy revision.]
**Detection.** [`/health/ready` returns 503, pods `0/1 Ready`, Argo CD Progressing or Degraded.]
**Recovery.** [Git revert; Argo CD restored the last good revision; committed orders were not lost.]
**Action items.** [Owner: Person C.]

## Evidence index (fill as you save the files)
| Item | File or command |
|------|-----------------|
| Worker logs before, during, after | `docs/evidence/[ ]` |
| `/metrics` before, during, after | `docs/evidence/[ ]` |
| SQL snapshots (queries below) | `docs/evidence/[ ]` |
| 30 s rollout probe CSV | `docs/evidence/[ ]` |
| Argo CD screenshots, rollout history, Git revisions | `docs/evidence/[ ]` |

## Queries for the data-integrity checks
```sql
-- orders and jobs by state
SELECT status, count(*) FROM orders GROUP BY status ORDER BY 1;
SELECT state, count(*) AS jobs, sum(attempts) AS attempts FROM jobs GROUP BY state ORDER BY 1;

-- no committed order without a job; no completed order whose job is not done
SELECT count(*) AS orders_without_job
FROM orders o LEFT JOIN jobs j ON j.order_id = o.id WHERE j.id IS NULL;
SELECT count(*) AS completed_but_job_not_done
FROM orders o JOIN jobs j ON j.order_id = o.id WHERE o.status = 'completed' AND j.state <> 'done';

-- stock check: seed minus available must equal everything ever reserved (failed orders keep theirs)
SELECT p.sku, p.available, coalesce(sum(i.quantity), 0) AS reserved
FROM products p LEFT JOIN order_items i ON i.sku = p.sku GROUP BY p.sku, p.available ORDER BY 1;
```
