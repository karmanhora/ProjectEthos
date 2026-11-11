"""Immutable domain models for a Project Ethos environment.

Ten entity models mirror the ten entity tables. `Environment` carries the state
version and the event-log head. `EnvironmentState` is the single aggregate that
hashing, diffing, simulation and replay all operate on.

No model performs I/O and no model generates a timestamp: every timestamp is
supplied by the caller from the injected Clock.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import Field

from ethos.runtime import FrozenModel, canonical_json, sha256_hex, to_plain

__all__ = [
    "OrgRole", "TeamRole", "ProjectRole", "ProjectStatus", "TaskStatus",
    "TaskPriority", "TaskType", "EnvironmentRules",
    "Organization", "EnvironmentUser", "Team", "TeamMembership", "Project",
    "ProjectMembership", "Task", "TaskAssignment", "TaskDependency",
    "Environment", "EnvironmentState",
    "EntityType", "ChangeOp", "FieldChange", "EntityChange", "ChangeSet",
    "DIGEST_FIELDS", "digest_of", "EnvironmentEvent", "EnvironmentSnapshot",
]


class OrgRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class TeamRole(str, Enum):
    LEAD = "lead"
    MEMBER = "member"


class ProjectRole(str, Enum):
    MANAGER = "manager"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class ProjectStatus(str, Enum):
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskType(str, Enum):
    TASK = "task"
    BUG = "bug"
    FEATURE = "feature"
    CHORE = "chore"


class EnvironmentRules(FrozenModel):
    """Explicit, documented business rules. Defaults are the Phase 1 ruleset."""

    schema_version: int = 1
    max_assignees_per_task: int = Field(default=1, ge=1)
    prevent_dependency_cycles: bool = True
    require_assignee_project_member: bool = True
    require_project_member_org_membership: bool = True
    block_completion_with_open_dependencies: bool = True
    preserve_team_lead: bool = True
    max_tasks_per_project: int | None = Field(default=None, ge=1)
    snapshot_interval: int = Field(default=25, ge=1)


class Organization(FrozenModel):
    id: UUID
    environment_id: UUID
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=40)
    created_at: datetime


class EnvironmentUser(FrozenModel):
    """A fictional actor inside one simulated environment.

    Deliberately not an authentication identity: no email, no password, no
    relation to any auth schema.
    """

    id: UUID
    environment_id: UUID
    org_id: UUID
    handle: str = Field(min_length=3, max_length=40)
    display_name: str = Field(min_length=1, max_length=120)
    org_role: OrgRole = OrgRole.MEMBER
    is_active: bool = True
    is_synthetic: bool = True
    created_at: datetime


class Team(FrozenModel):
    id: UUID
    environment_id: UUID
    org_id: UUID
    key: str = Field(min_length=2, max_length=10)
    name: str = Field(min_length=1, max_length=120)
    created_at: datetime


class TeamMembership(FrozenModel):
    id: UUID
    environment_id: UUID
    team_id: UUID
    user_id: UUID
    role: TeamRole = TeamRole.MEMBER
    added_by_id: UUID | None = None
    added_at: datetime
    removed_at: datetime | None = None
    removed_by_id: UUID | None = None


class Project(FrozenModel):
    id: UUID
    environment_id: UUID
    org_id: UUID
    key: str = Field(min_length=2, max_length=10)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    lead_user_id: UUID | None = None
    created_by_id: UUID
    started_on: date | None = None
    target_on: date | None = None
    created_at: datetime
    updated_at: datetime


class ProjectMembership(FrozenModel):
    id: UUID
    environment_id: UUID
    project_id: UUID
    user_id: UUID
    role: ProjectRole = ProjectRole.CONTRIBUTOR
    added_by_id: UUID | None = None
    added_at: datetime
    removed_at: datetime | None = None


class Task(FrozenModel):
    id: UUID
    environment_id: UUID
    project_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: TaskStatus = TaskStatus.TODO
    priority: TaskPriority = TaskPriority.MEDIUM
    task_type: TaskType = TaskType.TASK
    created_by_id: UUID
    due_on: date | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskAssignment(FrozenModel):
    id: UUID
    environment_id: UUID
    task_id: UUID
    user_id: UUID
    assigned_by_id: UUID
    assigned_at: datetime
    removed_at: datetime | None = None


class TaskDependency(FrozenModel):
    id: UUID
    environment_id: UUID
    task_id: UUID
    depends_on_task_id: UUID
    created_by_id: UUID
    created_at: datetime


class Environment(FrozenModel):
    """The environment's own content: identity, rules and content version.

    The event-log head is deliberately *not* here. `state_hash` covers this
    object, and every event records the hash of the state it produced, so a head
    pointer stored here would make event N's digest depend on a state containing
    event N's own digest - a cycle that cannot be computed. The head therefore
    lives beside the log, on `repository.EnvironmentHead`.
    """

    id: UUID
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    config: EnvironmentRules = EnvironmentRules()
    version: int = Field(ge=1)
    status: Literal["active", "archived"] = "active"
    created_at: datetime
    updated_at: datetime


class EnvironmentState(FrozenModel):
    """The whole environment: one immutable object, the unit of every operation."""

    environment: Environment
    organizations: dict[UUID, Organization] = {}
    users: dict[UUID, EnvironmentUser] = {}
    teams: dict[UUID, Team] = {}
    team_memberships: dict[UUID, TeamMembership] = {}
    projects: dict[UUID, Project] = {}
    project_memberships: dict[UUID, ProjectMembership] = {}
    tasks: dict[UUID, Task] = {}
    task_assignments: dict[UUID, TaskAssignment] = {}
    task_dependencies: dict[UUID, TaskDependency] = {}

    @property
    def environment_id(self) -> UUID:
        return self.environment.id

    @property
    def version(self) -> int:
        return self.environment.version


class EntityType(str, Enum):
    ENVIRONMENT = "environment"
    ORGANIZATION = "organization"
    ENVIRONMENT_USER = "environment_user"
    TEAM = "team"
    TEAM_MEMBERSHIP = "team_membership"
    PROJECT = "project"
    PROJECT_MEMBERSHIP = "project_membership"
    TASK = "task"
    TASK_ASSIGNMENT = "task_assignment"
    TASK_DEPENDENCY = "task_dependency"


class ChangeOp(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class FieldChange(FrozenModel):
    field: str
    before: Any = None
    after: Any = None


class EntityChange(FrozenModel):
    """One entity transition.

    `create` and `delete` carry every column of the entity; `update` carries
    exactly the differing fields with both values. That losslessness is what
    lets a recorded change set be replayed without reading anything else.
    """

    op: ChangeOp
    entity_type: EntityType
    entity_id: UUID
    fields: list[FieldChange] = []
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class ChangeSet(FrozenModel):
    changes: list[EntityChange] = []

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in self.changes:
            key = f"{change.entity_type.value}.{change.op.value}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def changed_entity_ids(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for change in self.changes:
            grouped.setdefault(change.entity_type.value, []).append(
                str(change.entity_id)
            )
        return grouped


DIGEST_FIELDS: tuple[str, ...] = (
    "id",
    "environment_id",
    "sequence",
    "action_type",
    "actor_user_id",
    "origin",
    "parameters",
    "changes",
    "status",
    "failure_code",
    "failure_reason",
    "failure_details",
    "prev_state_version",
    "prev_state_hash",
    "resulting_state_version",
    "resulting_state_hash",
    "idempotency_key",
    "request_fingerprint",
    "created_at",
    "prev_event_digest",
)


def digest_of(values: Mapping[str, Any]) -> str:
    """Canonical digest over the immutable fields of an event.

    Used both at insert time (before `event_digest` exists) and at verification
    time (recomputed from stored values).
    """
    payload = {name: to_plain(values[name]) for name in DIGEST_FIELDS}
    return sha256_hex(canonical_json(payload))


class EnvironmentEvent(FrozenModel):
    id: UUID
    environment_id: UUID
    sequence: int = Field(ge=1)
    action_type: str
    actor_user_id: UUID | None = None
    origin: Literal["bootstrap", "api"] = "api"
    parameters: dict[str, Any] = {}
    changes: ChangeSet = ChangeSet()
    status: Literal["succeeded", "failed"]
    failure_code: str | None = None
    failure_reason: str | None = None
    failure_details: dict[str, Any] | None = None
    prev_state_version: int = Field(ge=0)
    prev_state_hash: str = Field(min_length=64, max_length=64)
    resulting_state_version: int | None = None
    resulting_state_hash: str | None = None
    idempotency_key: str
    request_fingerprint: str = Field(min_length=64, max_length=64)
    prev_event_digest: str = Field(min_length=64, max_length=64)
    event_digest: str = Field(min_length=64, max_length=64)
    created_at: datetime

    def compute_digest(self) -> str:
        return digest_of(to_plain(self))


class EnvironmentSnapshot(FrozenModel):
    id: UUID
    environment_id: UUID
    version: int = Field(ge=1)
    last_event_sequence: int = Field(ge=1)
    state: EnvironmentState
    state_hash: str = Field(min_length=64, max_length=64)
    kind: Literal["bootstrap", "auto", "manual"] = "auto"
    label: str | None = None
    created_at: datetime
