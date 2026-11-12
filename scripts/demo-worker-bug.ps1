# Demo (worker): break the completion guard on purpose, show the test catching it, then restore it.
# The bug: the completion checks WHO holds the job but not WHICH ATTEMPT, so a worker that already
# lost its lease (same worker id, older attempt) could still finish the job.
# Run from the repo root in PowerShell with $env:DATABASE_URL pointing at the isolated TEST database.
$file  = "src/fulfillops/worker/queue.py"
$patch = "docs/demo/worker-lost-lease.patch"
$tests = @("tests/test_worker.py", "-k", "lost_its_lease or old_attempt")

if (-not $env:DATABASE_URL) { Write-Host "Set DATABASE_URL to the test database first."; exit 1 }
git diff --quiet -- $file
if ($LASTEXITCODE -ne 0) { Write-Host "$file has uncommitted changes. Commit or stash them first."; exit 1 }

Write-Host "=== 1. Correct code: completion needs id, state, worker id AND attempt number"
uv run pytest @tests -q -p no:cacheprovider 2>&1 | Select-Object -Last 3

Write-Host ""
Write-Host "=== 2. Deliberate bug: the attempt number is no longer checked"
git apply --ignore-whitespace $patch
if ($LASTEXITCODE -ne 0) { Write-Host "patch did not apply"; exit 1 }
try {
    git diff --stat -- $file
    uv run pytest @tests -q -p no:cacheprovider 2>&1 |
        Select-String -Pattern "^FAILED|AssertionError|assert |passed|failed" | Select-Object -First 6
}
finally {
    Write-Host ""
    Write-Host "=== 3. Restoring the correct code"
    git apply -R --ignore-whitespace $patch
    git diff --quiet -- $file
    if ($LASTEXITCODE -eq 0) { Write-Host ">> $file is back to the committed version" }
}
uv run pytest @tests -q -p no:cacheprovider 2>&1 | Select-Object -Last 3
