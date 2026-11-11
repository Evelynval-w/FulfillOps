"""Run the API with: python -m fulfillops.api

On SIGTERM uvicorn stops accepting connections, lets requests that are already
running finish, then closes the database pool. The timeout below is shorter than
the 30 s Kubernetes gives a pod, so a stuck request can never block the shutdown.
"""

import uvicorn

from fulfillops.api.app import create_app
from fulfillops.common.settings import load_settings

GRACEFUL_SHUTDOWN_SECONDS = 20

if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
