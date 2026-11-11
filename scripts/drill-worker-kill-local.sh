#!/bin/bash
cd .
export DATABASE_URL=postgresql://postgres:test@localhost:5432/e2e APP_ENV=test PROCESSING_SECONDS=6
for p in $(pgrep -f "[f]ulfillops.worker"); do kill $p; done; sleep 2
for w in w1 w2; do WORKER_ID=$w .venv/bin/python -m fulfillops.worker >/tmp/$w.log 2>&1 </dev/null & echo $! > /tmp/$w.pid; done
sleep 2
OID=$(curl -s -X POST localhost:8081/api/v1/orders -H 'Idempotency-Key: crash-drill-0001' -H 'Content-Type: application/json' -d '{"customer_id":"CUST-DRILL","items":[{"sku":"PART-200","quantity":1}]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["order_id"])')
q() { su postgres -c "psql -d e2e -Atc \"$1\""; }
echo "order $OID"; sleep 1.5
OWNER=$(q "select locked_by from jobs where order_id='$OID'")
echo "claimed by $OWNER -> kill -9 pid $(cat /tmp/$OWNER.pid)"; kill -9 $(cat /tmp/$OWNER.pid)
for i in 1 3 6 9 12 15; do sleep 2; echo "after ~${i}x2s: $(q "select o.status||' | job='||j.state||' attempts='||j.attempts||' by='||coalesce(j.locked_by,'-') from orders o join jobs j on j.order_id=o.id where o.id='$OID'")"; done
grep -h "$OID" /tmp/w1.log /tmp/w2.log | cut -c12-160
echo "stock PART-200: $(q "select available from products where sku='PART-200'")"
