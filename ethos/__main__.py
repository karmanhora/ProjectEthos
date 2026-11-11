"""`python -m ethos` - run the HTTP API.

`--reload` is a development convenience: uvicorn then re-imports the factory in
a child process whenever a file under `ethos/` changes. That requires an import
string rather than an app object, which is why the two branches differ.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from ethos.api.app import create_app
from ethos.config import Settings, load_environment

PACKAGE_DIR = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m ethos", description="Run the Project Ethos HTTP API."
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart when files under ethos/ change (development only)",
    )
    args = parser.parse_args(argv)

    # Load before reading settings, and before uvicorn forks for --reload, so the
    # child process inherits the same values.
    load_environment()
    settings = Settings.from_env()

    if args.reload:
        # uvicorn cannot watch an app instance: it must import the factory itself.
        uvicorn.run(
            "ethos.api.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            reload=True,
            # Watching only the package keeps .venv out of the file watcher,
            # which is both slow and prone to spurious restarts.
            reload_dirs=[str(PACKAGE_DIR)],
        )
        return

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
