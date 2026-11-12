#!/usr/bin/env bash
# Demo (worker): break the completion guard on purpose, show the test catching it, then restore it.
# The bug: the completion checks WHO holds the job but not WHICH ATTEMPT, so a worker that already
# lost its lease (same worker id, older attempt) could still finish the job.
# Run from the repo root with DATABASE_URL pointing at the isolated TEST database.
set -u
FILE=src/fulfillops/worker/queue.py
PATCH=docs/demo/worker-lost-lease.patch
TESTS=(tests/test_worker.py -k "lost_its_lease or old_attempt")

if [ -z "${DATABASE_URL:-}" ]; then echo "Set DATABASE_URL to the test database first."; exit 1; fi
if ! git diff --quiet -- "$FILE"; then echo "$FILE has uncommitted changes. Commit or stash them first."; exit 1; fi

restore() { git apply -R --ignore-whitespace "$PATCH" 2>/dev/null && echo ">> code restored"; }
trap restore EXIT

echo "=== 1. Correct code: completion needs id, state, worker id AND attempt number"
uv run pytest "${TESTS[@]}" -q -p no:cacheprovider 2>&1 | tail -3

echo
echo "=== 2. Deliberate bug: the attempt number is no longer checked"
git apply --ignore-whitespace "$PATCH" || { echo "patch did not apply"; exit 1; }
git diff --stat -- "$FILE"
uv run pytest "${TESTS[@]}" -q -p no:cacheprovider 2>&1 | grep -E "^FAILED|AssertionError|assert |passed|failed" | head -6

echo
echo "=== 3. Restoring the correct code"
restore; trap - EXIT
git diff --quiet -- "$FILE" && echo ">> $FILE is back to the committed version"
uv run pytest "${TESTS[@]}" -q -p no:cacheprovider 2>&1 | tail -3
