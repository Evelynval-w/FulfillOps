#!/usr/bin/env bash
# Demo: break the stock reservation on purpose, show the race test catching it, then restore it.
# Run from the repo root with DATABASE_URL pointing at the isolated TEST database.
set -u
FILE=src/fulfillops/api/orders.py
PATCH=docs/demo/naive-reservation.patch
TESTS="tests/test_orders.py -k race"

if [ -z "${DATABASE_URL:-}" ]; then echo "Set DATABASE_URL to the test database first."; exit 1; fi
if ! git diff --quiet -- "$FILE"; then echo "$FILE has uncommitted changes. Commit or stash them first."; exit 1; fi

restore() { git apply -R "$PATCH" 2>/dev/null && echo ">> code restored"; }
trap restore EXIT

echo "=== 1. Correct code: one conditional UPDATE per SKU"
python -m pytest $TESTS -q -p no:cacheprovider 2>&1 | tail -3

echo
echo "=== 2. Deliberate bug: read the stock, check it in Python, write it back"
git apply "$PATCH" || { echo "patch did not apply"; exit 1; }
git diff --stat -- "$FILE"
python -m pytest $TESTS -q -p no:cacheprovider 2>&1 | grep -E "AssertionError|assert|passed|failed" | head -6

echo
echo "=== 3. Restoring the correct code"
restore; trap - EXIT
git diff --quiet -- "$FILE" && echo ">> $FILE is back to the committed version"
python -m pytest $TESTS -q -p no:cacheprovider 2>&1 | tail -3
