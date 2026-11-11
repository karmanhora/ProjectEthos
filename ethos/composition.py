"""The composition root: the one place that decides which implementation runs.

Nothing else in the codebase chooses between the in-memory and Postgres
repositories, and nothing else constructs a service. That keeps the choice
visible and testable instead of being scattered across call sites.
"""
from __future__ import annotations

from uuid import UUID

from ethos.config import Settings
from ethos.repository import EnvironmentRepository, InMemoryRepository
from ethos.runtime import Clock, IdFactory, SystemClock, UuidFactory
from ethos.service import EnvironmentService

__all__ = ["build_repository", "build_service"]


def build_repository(settings: Settings) -> EnvironmentRepository:
    """Postgres when a connection string is configured, in memory otherwise.

    The fallback is deliberate rather than silent: `/health` reports which
    implementation is live, so a process that lost its database URL cannot look
    healthy while quietly discarding state on exit.
    """
    if settings.database_url:
        from ethos.postgres_repository import PostgresRepository

        return PostgresRepository(settings.database_url)
    return InMemoryRepository()


def build_service(
    repository: EnvironmentRepository,
    environment_id: UUID,
    *,
    clock: Clock | None = None,
    ids: IdFactory | None = None,
) -> EnvironmentService:
    return EnvironmentService(
        repository,
        environment_id,
        clock=clock or SystemClock(),
        ids=ids or UuidFactory(),
    )
