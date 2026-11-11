"""Immutable primitives, deterministic time and IDs, and canonical serialization.

Pydantic's `frozen=True` does NOT protect nested collections: validation coerces
`Mapping[K, V]` fields back to a mutable dict. `FrozenModel.model_post_init`
re-freezes every field after validation, which is what makes the guarantee real.

`FrozenMapping` stores a `types.MappingProxyType`, so its contents are read-only
at the C level rather than by an overridden setter. Values are frozen recursively
at construction, so a nested dict is immutable through any reference to it.

`model_dump()` cannot serialize `FrozenMapping` and must never be used on domain
objects. `to_plain()` is the single serializer.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict

__all__ = [
    "FrozenMapping", "FrozenModel", "freeze", "to_plain", "canonical_json",
    "sha256_hex", "GENESIS_STATE_HASH", "GENESIS_EVENT_DIGEST",
    "Clock", "SystemClock", "FixedClock", "IdFactory", "UuidFactory",
    "DeterministicIdFactory",
]


class FrozenMapping(Mapping):
    """Read-only mapping backed by a MappingProxyType.

    Values are frozen recursively at construction, so nested containers are
    immutable too. Data is assumed acyclic, which the domain guarantees.
    """

    __slots__ = ("_proxy",)

    def __init__(self, data: Mapping | None = None) -> None:
        frozen_values = {k: freeze(v) for k, v in (data or {}).items()}
        object.__setattr__(self, "_proxy", MappingProxyType(frozen_values))

    def __getitem__(self, key: Any) -> Any:
        return self._proxy[key]

    def __iter__(self) -> Iterator:
        return iter(self._proxy)

    def __len__(self) -> int:
        return len(self._proxy)

    def __repr__(self) -> str:
        return f"FrozenMapping({dict(self._proxy)!r})"

    def __deepcopy__(self, memo: dict | None = None) -> "FrozenMapping":
        # deepcopy of a mappingproxy raises TypeError; immutability makes
        # sharing the same object safe, so identity is the correct semantics.
        return self

    def __setattr__(self, key: str, value: Any) -> None:
        raise TypeError("FrozenMapping is immutable")


def freeze(value: Any) -> Any:
    """Recursively convert mutable containers into immutable equivalents."""
    if isinstance(value, FrozenMapping):
        return value
    if isinstance(value, Mapping):
        return FrozenMapping(value)  # __init__ freezes values recursively
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(v) for v in value)
    return value


class FrozenModel(BaseModel):
    """Base for every domain object. Deeply immutable, no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def model_post_init(self, _context: Any) -> None:
        # Runs after validation because Pydantic has already replaced our frozen
        # mappings with mutable dicts. object.__setattr__ is Pydantic's documented
        # escape hatch inside frozen models.
        for name in type(self).model_fields:
            object.__setattr__(self, name, freeze(getattr(self, name)))


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime rejected: every timestamp must be timezone-aware UTC"
        )
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def to_plain(value: Any) -> Any:
    """Convert to plain JSON-compatible data. Always returns fresh containers."""
    if isinstance(value, Mapping):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, frozenset):
        items = [to_plain(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, BaseModel):
        return {n: to_plain(getattr(value, n)) for n in type(value).model_fields}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no insignificant whitespace. The only hashing input."""
    return json.dumps(
        to_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


GENESIS_STATE_HASH = sha256_hex(b"ethos:genesis:v1")
GENESIS_EVENT_DIGEST = sha256_hex(b"ethos:event-chain:genesis:v1")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Test clock. Optionally advances by `step` on each call."""

    def __init__(self, instant: datetime, step: timedelta | None = None) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware instant")
        self._instant = instant.astimezone(timezone.utc)
        self._step = step

    def now(self) -> datetime:
        current = self._instant
        if self._step is not None:
            self._instant = self._instant + self._step
        return current


class IdFactory(Protocol):
    def new_id(self) -> UUID: ...


class UuidFactory:
    def new_id(self) -> UUID:
        return uuid4()


class DeterministicIdFactory:
    """Reproducible ids for tests: uuid5 over a namespace and a counter."""

    def __init__(self, namespace: UUID, start: int = 1) -> None:
        self._namespace = namespace
        self._counter = start

    def new_id(self) -> UUID:
        value = uuid5(self._namespace, str(self._counter))
        self._counter += 1
        return value
