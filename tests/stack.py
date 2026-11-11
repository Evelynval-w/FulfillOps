"""Helpers that start the real API and worker as separate processes (used by the slow tests)."""

import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"

# uvicorn finishes its graceful shutdown and then re-raises SIGTERM, so a clean stop
# shows up as 0 or as -15 depending on the uvicorn version. Both mean "shut down properly".
CLEAN_EXIT = (0, -15)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if check():
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    return response.status, response.read()


def base_env(database_url: str, **extra) -> dict:
    return {
        **os.environ,
        "DATABASE_URL": database_url,
        "APP_ENV": "test",
        "MIGRATIONS_DIR": str(MIGRATIONS),
        "PYTHONPATH": str(ROOT / "src"),
        **{k: str(v) for k, v in extra.items()},
    }


class Stack:
    """One test's API and workers. Output goes to files so a failure can show the logs."""

    def __init__(self, database_url: str, log_dir: Path):
        self.database_url = database_url
        self.log_dir = log_dir
        self.port = free_port()
        self.procs: dict[str, subprocess.Popen] = {}

    def _start(self, name: str, module: str, **env) -> subprocess.Popen:
        log = open(self.log_dir / f"{name}.log", "w")
        proc = subprocess.Popen(
            [sys.executable, "-m", module],
            env=base_env(self.database_url, **env),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.procs[name] = proc
        return proc

    def start_api(self, **env) -> subprocess.Popen:
        proc = self._start("api", "fulfillops.api", PORT=self.port, **env)
        ok = wait_until(lambda: call(self.port, "GET", "/health/live")[0] == 200)
        assert ok, "API did not start\n" + self.logs()
        return proc

    def start_worker(self, name: str, **env) -> subprocess.Popen:
        return self._start(name, "fulfillops.worker", WORKER_ID=name, **env)

    def get(self, path):
        return call(self.port, "GET", path)

    def post_order(self, key, customer="CUST-1", sku="PART-200", quantity=1):
        import json

        body = json.dumps({"customer_id": customer, "items": [{"sku": sku, "quantity": quantity}]})
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        return call(self.port, "POST", "/api/v1/orders", body, headers)

    def logs(self) -> str:
        out = []
        for path in sorted(self.log_dir.glob("*.log")):
            lines = path.read_text().splitlines()[-25:]
            out.append(f"--- {path.name} ---\n" + "\n".join(lines))
        return "\n".join(out)

    def stop_all(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.kill()
            proc.wait()
