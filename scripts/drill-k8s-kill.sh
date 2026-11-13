#!/usr/bin/env bash
# Worker kill drill on the Kubernetes cluster (TEST cluster only).
#
# Kills the worker pod that holds a lease, with no grace period (a crash, not a clean stop), and
# shows another worker finishing the order once. Saves logs, metrics and SQL snapshots.
#
# Before you run it:
#   1. At least one SKU has stock left (the script picks the SKU with the most stock; no reset needed).
#   2. The workers run with PROCESSING_SECONDS=5 (default is 0.5, too short to kill a worker mid-job).
#      Argo CD self-heal reverts a manual change: pause auto-sync first (see the instructions in chat).
#   3. kubectl points at the cluster, and the API is reachable at $API (default: the service
#      port-forward on 18083).
#
#   NS=fulfillops API=http://127.0.0.1:18083 bash scripts/drill-k8s-kill.sh
#
# Not run against a real cluster by its author: check the output of the first lines (names found).
set -u
NS="${NS:-fulfillops}"
API="${API:-http://127.0.0.1:18083}"
OUT="${OUT:-docs/evidence}"
mkdir -p "$OUT"

PG="$(kubectl get pods -n "$NS" -o name | grep -i postgres | head -1)"
if [ -z "$PG" ]; then echo "No postgres pod found in namespace $NS"; exit 1; fi
PGUSER="$(kubectl exec -n "$NS" "$PG" -- printenv POSTGRES_USER)"
PGDB="$(kubectl exec -n "$NS" "$PG" -- printenv POSTGRES_DB)"
q() { kubectl exec -n "$NS" "$PG" -- psql -U "$PGUSER" -d "$PGDB" -At -c "$1"; }
metrics() { curl -s "$API/metrics" | grep -E '^fulfillops_(orders|worker)'; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "namespace=$NS  postgres=$PG  db=$PGDB  api=$API"
echo "--- worker pods:"; kubectl get pods -n "$NS" | grep -i worker
WORKERS="$(kubectl get pods -n "$NS" -o name | grep -i worker)"
FIRST="$(echo "$WORKERS" | head -1)"
PS="$(kubectl exec -n "$NS" "$FIRST" -- printenv PROCESSING_SECONDS 2>/dev/null || echo unset)"
echo "PROCESSING_SECONDS in the workers: $PS"
case "$PS" in 5|5.0|6|7|8|10) ;; *) echo "WARNING: set PROCESSING_SECONDS to 5 or more first, or the job ends before the kill."; read -r -p "Continue anyway? [y/N] " a; [ "$a" = "y" ] || exit 1;; esac

echo "--- before"
metrics | tee "$OUT/k8s-kill-metrics-before.txt"
q "SELECT status, count(*) FROM orders GROUP BY 1 ORDER BY 1" | tee "$OUT/k8s-kill-sql-before.txt"
q "SELECT sku, available FROM products ORDER BY 1" | tee -a "$OUT/k8s-kill-sql-before.txt"

SKU="${SKU:-$(q "SELECT sku FROM products WHERE available > 0 ORDER BY available DESC LIMIT 1")}"
[ -n "$SKU" ] || { echo "No SKU has stock left: ask for a database reset."; exit 1; }
echo "ordering 1 x $SKU"
KEY="k8s-kill-$(date +%s)"
RESP="$(curl -s -X POST "$API/api/v1/orders" -H "Idempotency-Key: $KEY" -H 'Content-Type: application/json' \
  -d "{\"customer_id\":\"CUST-K8S-KILL\",\"items\":[{\"sku\":\"$SKU\",\"quantity\":1}]}")"
OID="$(echo "$RESP" | sed -n 's/.*"order_id" *: *"\([^"]*\)".*/\1/p')"
echo "order: $OID   (response: $RESP)"
[ -n "$OID" ] || { echo "Order was not created (out of stock?)"; exit 1; }

sleep 1.5
OWNER="$(q "SELECT locked_by FROM jobs WHERE order_id='$OID'")"
echo "claimed by: $OWNER"
if ! kubectl get pod -n "$NS" "$OWNER" >/dev/null 2>&1; then
  echo "locked_by '$OWNER' is not a pod name: check how WORKER_ID is set in the worker Deployment."; exit 1
fi

kubectl logs -f -n "$NS" "$OWNER" --timestamps > "$OUT/k8s-kill-owner-log.txt" 2>&1 &
LOGPID=$!
sleep 1
echo "$(now)  killing $OWNER with no grace period" | tee "$OUT/k8s-kill-time.txt"
kubectl delete pod -n "$NS" "$OWNER" --grace-period=0 --force

echo "--- progress (UTC)" | tee "$OUT/k8s-kill-progress.txt"
for _ in $(seq 1 15); do
  sleep 2
  echo "$(date -u +%H:%M:%S)  $(q "SELECT o.status||'|'||j.state||'|'||j.attempts||'|'||coalesce(j.locked_by,'') FROM orders o JOIN jobs j ON j.order_id=o.id WHERE o.id='$OID'")" | tee -a "$OUT/k8s-kill-progress.txt"
done
kill "$LOGPID" 2>/dev/null

echo "--- after"
metrics | tee "$OUT/k8s-kill-metrics-after.txt"
for p in $(kubectl get pods -n "$NS" -o name | grep -i worker); do
  echo "=== $p"; kubectl logs -n "$NS" "$p" --timestamps
done > "$OUT/k8s-kill-worker-logs.txt" 2>&1
kubectl get pods -n "$NS" | tee "$OUT/k8s-kill-pods-after.txt"

{
  echo "orders by status:";                     q "SELECT status, count(*) FROM orders GROUP BY 1 ORDER BY 1"
  echo "jobs by state (count|attempts):";       q "SELECT state, count(*), sum(attempts) FROM jobs GROUP BY 1 ORDER BY 1"
  echo "orders without a job:";                 q "SELECT count(*) FROM orders o LEFT JOIN jobs j ON j.order_id=o.id WHERE j.id IS NULL"
  echo "completed orders whose job is not done:"; q "SELECT count(*) FROM orders o JOIN jobs j ON j.order_id=o.id WHERE o.status='completed' AND j.state<>'done'"
  echo "stock:";                                q "SELECT sku, available FROM products ORDER BY 1"
} | tee "$OUT/k8s-kill-integrity.txt"
echo "Done. Evidence in $OUT/k8s-kill-*.txt"
