"""Application factory.

The repository and the clock are created once and shared, because the clock must
monotonic across requests and the repository is the process's whole memory of
the world.
"""
from __future__ import annotations

from fastapi import FastAPI

from ethos.api.errors import install_error_handlers
from ethos.api.routes import router
from ethos.composition import build_repository
from ethos.config import Settings, load_environment
from ethos.runtime import SystemClock, UuidFactory

__all__ = ["create_app"]


def create_app(settings: Settings | None = None) -> FastAPI:
    # Only when settings are read from the environment: an explicit Settings
    # object is a caller's decision and must not be second-guessed by a file.
    if settings is None:
        load_environment()
    resolved = settings or Settings.from_env()

    app = FastAPI(
        title="Project Ethos",
        version="0.1.0",
        description=(
            "A deterministic project-management environment with a tamper-evident "
            "event log. Every action is validated, recorded and committed as one "
            "event."
        ),
    )
    app.state.settings = resolved
    app.state.repository = build_repository(resolved)
    app.state.clock = SystemClock()
    app.state.ids = UuidFactory()

    install_error_handlers(app)
    app.include_router(router)

    @app.get("/", tags=["meta"])
    def index() -> dict[str, object]:
        # Without this, opening the server root returns FastAPI's bare 404, which
        # reads like a broken deployment rather than "the API lives under /v1".
        return {
            "service": "project-ethos",
            "version": "0.1.0",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
            "endpoints": {
                "create_environment": "POST /v1/environments",
                "read_environment": "GET /v1/environments/{environment_id}",
                "run_action": "POST /v1/environments/{environment_id}/actions",
                "list_events": "GET /v1/environments/{environment_id}/events",
                "integrity": "GET /v1/environments/{environment_id}/integrity",
            },
        }

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        # Naming the repository makes a silent in-memory fallback visible.
        return {
            "status": "ok",
            "repository": type(app.state.repository).__name__,
            "environment": resolved.env,
        }

    return app
