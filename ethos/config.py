"""Process configuration. Secrets are read from the environment and never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

__all__ = ["Settings"]


def _flag(source: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = source.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    database_url: str | None = None
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    api_key: str | None = None
    snapshot_interval: int = 25
    max_simulation_actions: int = 50
    test_database_url: str | None = None
    allow_integration_writes: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if environ is None else environ

        def text(name: str, default: str | None = None) -> str | None:
            value = source.get(name)
            return default if value is None or value == "" else value

        def integer(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or raw == "":
                return default
            value = int(raw)
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
            return value

        return cls(
            env=text("ETHOS_ENV", "development") or "development",
            host=text("ETHOS_HOST", "127.0.0.1") or "127.0.0.1",
            port=integer("ETHOS_PORT", 8000),
            log_level=(text("ETHOS_LOG_LEVEL", "INFO") or "INFO").upper(),
            database_url=text("DATABASE_URL"),
            supabase_url=text("SUPABASE_URL"),
            supabase_anon_key=text("SUPABASE_ANON_KEY"),
            api_key=text("ETHOS_API_KEY"),
            snapshot_interval=integer("ETHOS_SNAPSHOT_INTERVAL", 25),
            max_simulation_actions=integer("ETHOS_MAX_SIMULATION_ACTIONS", 50),
            test_database_url=text("ETHOS_TEST_DATABASE_URL"),
            allow_integration_writes=_flag(source, "ETHOS_ALLOW_INTEGRATION_WRITES"),
        )

    def __repr__(self) -> str:  # never leak connection strings into logs
        return (
            f"Settings(env={self.env!r}, host={self.host!r}, port={self.port}, "
            f"log_level={self.log_level!r}, "
            f"database_url={'set' if self.database_url else 'unset'}, "
            f"supabase_url={'set' if self.supabase_url else 'unset'}, "
            f"api_key={'set' if self.api_key else 'unset'})"
        )
