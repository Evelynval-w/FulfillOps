"""Run the API with: python -m fulfillops.api"""

import uvicorn

from fulfillops.api.app import create_app
from fulfillops.common.settings import load_settings

if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)
