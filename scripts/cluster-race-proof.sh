#!/usr/bin/env bash
# Cross-replica race proof on the kind cluster (acceptance gate 1).
#   1. 8 simultaneous POSTs with ONE key, spread over the two API pods -> one order, stock down once
#   2. 8 simultaneous POSTs with DIFFERENT keys for the remaining stock -> only the stock available is sold
#   3. delete one API pod, wait for the replacement, replay the key -> same order, stock unchanged
# It consumes PACK-300 stock, so it refuses to run unless the stock is still the seed value (5).
# It never resets or deletes data. Run it on a freshly deployed lab cluster only.
set -euo pipefail

NS=${NS:-fulfillops}
SKU=PACK-300
KEY="race-proof-$(date +%s)"
OUT="docs/evidence/cluster-race-$(date -u +%Y%m%dT%H%M%SZ).txt"
TMP=$(mktemp -d)
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; rm -rf "$TMP"; }
trap cleanup EXIT
mkdir -p docs/evidence
exec > >(tee "$OUT") 2>&1

psql_q() { kubectl -n "$NS" exec deploy/postgres -- sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""; }
pods() { kubectl -n "$NS" get pods -l app=fulfillops-api -o jsonpath='{.items[*].metadata.name}'; }
forward() { # forward <pod> <local port>
  kubectl -n "$NS" port-forward "pod/$1" "$2:8080" >/dev/null 2>&1 &
  PIDS+=("$!"); sleep 2
}
post() { # post <port> <key> <qty> <outfile>
  curl -s -o "$4" -w '%{http_code}' -X POST "localhost:$1/api/v1/orders" \
    -H "Idempotency-Key: $2" -H 'Content-Type: application/json' \
    -d "{\"customer_id\":\"CUST-RACE\",\"items\":[{\"sku\":\"$SKU\",\"quantity\":$3}]}" >"$4.code"
}
field() { python3 -c "
import sys, json
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
k = sys.argv[2]
print(d.get(k) if k in d else (d.get('error') or {}).get(k.replace('error_', ''), '-'))" "$1" "$2"; }

echo "== $(date -u +%FT%TZ) cluster race proof, key=$KEY"
read -r P1 P2 <<<"$(pods)"
[ -n "${P2:-}" ] || { echo "need two API pods, found: $(pods)"; exit 1; }
echo "API pods: $P1 $P2"
STOCK=$(psql_q "select available from products where sku='$SKU'")
[ "$STOCK" = "5" ] || { echo "stock of $SKU is $STOCK, not the seed value 5: use a freshly deployed lab database"; exit 1; }
forward "$P1" 18081; forward "$P2" 18082

echo; echo "== 1. eight simultaneous POSTs, same key, same payload (qty 2), four per pod"
for i in 1 2 3 4 5 6 7 8; do
  port=$((18081 + i % 2)); post "$port" "$KEY" 2 "$TMP/same$i" & 
done; wait
for i in 1 2 3 4 5 6 7 8; do echo "request $i: HTTP $(cat "$TMP/same$i.code") order_id=$(field "$TMP/same$i" order_id)"; done
echo "distinct order ids returned: $(for i in 1 2 3 4 5 6 7 8; do field "$TMP/same$i" order_id; done | sort -u | wc -l)"
echo "orders with this key in DB: $(psql_q "select count(*) from orders where idempotency_key='$KEY'")"
echo "stock $SKU: $(psql_q "select available from products where sku='$SKU'") (expected 3)"

echo; echo "== 2. eight simultaneous POSTs, different keys (qty 1), for the 3 units left"
for i in 1 2 3 4 5 6 7 8; do
  port=$((18081 + i % 2)); post "$port" "$KEY-c$i" 1 "$TMP/c$i" &
done; wait
for i in 1 2 3 4 5 6 7 8; do echo "request $i: HTTP $(cat "$TMP/c$i.code") $(field "$TMP/c$i" error_code)"; done
echo "accepted: $(grep -l '^202$' "$TMP"/c?.code | wc -l) (expected 3), refused: $(grep -L '^202$' "$TMP"/c?.code | wc -l) (expected 5)"
echo "stock $SKU: $(psql_q "select available from products where sku='$SKU'") (expected 0, never negative)"

echo; echo "== 3. restart one API pod, then replay the same key"
OID=$(field "$TMP/same1" order_id)
kubectl -n "$NS" delete pod "$P1" --wait=true
kubectl -n "$NS" wait --for=condition=Ready pod -l app=fulfillops-api --timeout=120s
NEW=$(pods | tr ' ' '\n' | grep -v "^$P2$" | head -1)
echo "replacement pod: $NEW"
forward "$NEW" 18083
post 18083 "$KEY" 2 "$TMP/replay"
echo "replay: HTTP $(cat "$TMP/replay.code") order_id=$(field "$TMP/replay" order_id) (first order was $OID)"
echo "orders with this key in DB: $(psql_q "select count(*) from orders where idempotency_key='$KEY'")"
echo "stock $SKU: $(psql_q "select available from products where sku='$SKU'") (still 0)"

echo; echo "== queries for the report"
psql_q "select o.id, o.status, o.idempotency_key from orders o where o.idempotency_key like '$KEY%' order by o.created_at"
echo "== done, evidence saved to $OUT"
