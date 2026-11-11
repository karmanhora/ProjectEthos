"""Wire models.

Deliberately separate from the domain models: these may change shape for
clients, while `ethos.models` is the hashed representation and must not.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActionRequestModel",
    "ActionResponse",
    "BootstrapRequest",
    "EnvironmentCreated",
    "EnvironmentSummary",
    "ErrorBody",
    "ErrorResponse",
    "EventSummary",
    "IntegrityResponse",
]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapRequest(APIModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    org_name: str = Field(min_length=1, max_length=120)
    org_slug: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$")
    owner_handle: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9_]+$")
    owner_display_name: str = Field(min_length=1, max_length=120)


class EnvironmentCreated(APIModel):
    environment_id: UUID
    owner_user_id: UUID
    version: int
    last_event_sequence: int
    last_event_digest: str
    event_id: UUID


class ActionRequestModel(APIModel):
    action_type: str = Field(min_length=1, max_length=80)
    actor_user_id: UUID
    parameters: dict[str, Any] = {}
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)


class EventSummary(APIModel):
    id: UUID
    sequence: int
    action_type: str
    actor_user_id: UUID | None
    origin: Literal["bootstrap", "api"]
    status: Literal["succeeded", "failed"]
    failure_code: str | None
    failure_reason: str | None
    resulting_state_version: int | None
    event_digest: str
    created_at: datetime


class ActionResponse(APIModel):
    version: int
    replayed: bool
    event: EventSummary
    changed: dict[str, list[str]]


class EnvironmentSummary(APIModel):
    environment_id: UUID
    name: str
    status: str
    version: int
    last_event_sequence: int
    last_event_digest: str
    counts: dict[str, int]


class IntegrityResponse(APIModel):
    valid: bool
    events_checked: int
    deep: bool
    final_version: int
    final_hash: str
    divergence: dict[str, Any] | None = None


class ErrorBody(APIModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(APIModel):
    error: ErrorBody
