import logging

import uvicorn

from app.config import settings
from app.logging_config import setup_logging

if __name__ == "__main__":
    setup_logging(logging.INFO)
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
        log_level="info",
    )
