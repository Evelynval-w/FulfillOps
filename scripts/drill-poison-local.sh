#!/bin/bash
cd .
export DATABASE_URL=postgresql://postgres:test@localhost:5432/e2e APP_ENV=test PROCESSING_SECONDS=0.5 POISON_CUSTOMER_ID=POISON-DEMO
for p in $(pgrep -f "[f]ulfillops.worker"); do kill $p; done; sleep 2
for w in w1 w2; do WORKER_ID=$w .venv/bin/python -m fulfillops.worker >/tmp/$w.log 2>&1 </dev/null & done
sleep 2
OID=$(curl -s -X POST localhost:8080/api/v1/orders -H 'Idempotency-Key: poison-drill-0001' -H 'Content-Type: application/json' -d '{"customer_id":"POISON-DEMO","items":[{"sku":"PART-200","quantity":1}]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["order_id"])')
echo "order $OID"
STOCK0=$(su postgres -c "psql -d e2e -Atc \"select available from products where sku='PART-200'\"")
sleep 45
su postgres -c "psql -d e2e -Atc \"select 'order='||o.status||' job='||j.state||' attempts='||j.attempts||' err='||j.last_error from orders o join jobs j on j.order_id=o.id where o.id='$OID'\""
echo "stock after order: $STOCK0, now: $(su postgres -c "psql -d e2e -Atc \"select available from products where sku='PART-200'\"")"
grep -h "$OID" /tmp/w1.log /tmp/w2.log | cut -c12-175 | sort
curl -s localhost:8080/metrics | grep -E "^fulfillops_(orders_failed|worker_retries|orders_created)_total"
for p in $(pgrep -f "[f]ulfillops.worker"); do kill -TERM $p; done; sleep 1; tail -1 /tmp/w1.log | cut -c12-80
