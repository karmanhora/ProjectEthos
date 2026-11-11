"""`python -m ethos` - run the HTTP API."""
from __future__ import annotations

import uvicorn

from ethos.api.app import create_app
from ethos.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
