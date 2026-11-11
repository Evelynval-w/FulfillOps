"""Start the worker with: python -m fulfillops.worker

SIGTERM and SIGINT stop the loop after the current job is finished.
"""

from __future__ import annotations

import logging
import signal
import threading

from fulfillops.worker.runner import run
from fulfillops.worker.settings import load_worker_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_worker_settings()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run(settings, stop)


if __name__ == "__main__":
    main()
