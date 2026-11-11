"""The action registry: the only path by which world state ever changes.

A handler is pure. It receives the current state plus already-validated
parameters and returns a new state at the *same* version; it never mutates its
input, never reads a clock of its own, and performs no I/O. The service derives
the recorded ChangeSet by diffing the returned state against the input, so a
handler cannot record history that disagrees with the state it produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from ethos.errors import FailureCode, RuleViolation, ValidationError
from ethos.models import (
    EnvironmentState,
    EnvironmentUser,
    Organization,
    OrgRole,
    Project,
    ProjectMembership,
    ProjectRole,
    ProjectStatus,
    Task,
    TaskAssignment,
    TaskDependency,
    TaskPriority,
    TaskStatus,
    TaskType,
    Team,
    TeamMembership,
    TeamRole,
)
from ethos.permissions import (
    Capability,
    active_project_membership,
    require_capability,
)
from ethos.runtime import IdFactory

__all__ = [
    "ActionContext",
    "ActionSpec",
    "ACTIONS",
    "get_action",
    "parse_parameters",
    "with_environment",
]


@dataclass(frozen=True)
class ActionContext:
    """Everything a handler may read. Note there is no clock: one timestamp
    per action is chosen by the service and shared by every change it makes."""

    state: EnvironmentState
    actor: EnvironmentUser
    now: datetime
    ids: IdFactory
    params: Any


# --- construction helpers ---------------------------------------------------


def _fields(model: Any) -> dict[str, Any]:
    return {name: getattr(model, name) for name in type(model).model_fields}


def _with_fields(model: Any, **changes: Any) -> Any:
    return type(model)(**{**_fields(model), **changes})


def _rebuild(state: EnvironmentState, **overrides: Any) -> EnvironmentState:
    return EnvironmentState(**{**_fields(state), **overrides})


def with_environment(state: EnvironmentState, **changes: Any) -> EnvironmentState:
    return _rebuild(state, environment=_with_fields(state.environment, **changes))


def _touch(state: EnvironmentState, now: datetime) -> EnvironmentState:
    return with_environment(state, updated_at=now)


def _store(state: EnvironmentState, attribute: str, entity: Any) -> EnvironmentState:
    bucket = dict(getattr(state, attribute))
    bucket[entity.id] = entity
    return _rebuild(state, **{attribute: bucket})


# --- parameter models -------------------------------------------------------


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrgCreateParams(_Params):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$")


class UserCreateParams(_Params):
    handle: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=120)
    org_role: OrgRole = OrgRole.MEMBER


class TeamCreateParams(_Params):
    key: str = Field(min_length=2, max_length=10)
    name: str = Field(min_length=1, max_length=120)


class TeamMemberAddParams(_Params):
    team_id: UUID
    user_id: UUID
    role: TeamRole = TeamRole.MEMBER


class ProjectCreateParams(_Params):
    key: str = Field(min_length=2, max_length=10)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    started_on: date | None = None
    target_on: date | None = None


class ProjectMemberAddParams(_Params):
    project_id: UUID
    user_id: UUID
    role: ProjectRole = ProjectRole.CONTRIBUTOR


class TaskCreateParams(_Params):
    project_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    task_type: TaskType = TaskType.TASK
    due_on: date | None = None


class TaskUpdateParams(_Params):
    task_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    priority: TaskPriority | None = None
    task_type: TaskType | None = None
    due_on: date | None = None


class TaskStatusParams(_Params):
    task_id: UUID
    status: TaskStatus


class TaskAssignParams(_Params):
    task_id: UUID
    user_id: UUID


class TaskUnassignParams(_Params):
    task_id: UUID
    user_id: UUID


class TaskDependencyAddParams(_Params):
    task_id: UUID
    depends_on_task_id: UUID


# --- lookups ----------------------------------------------------------------


def _not_found(kind: str, identifier: Any) -> RuleViolation:
    return RuleViolation(
        f"{kind} {identifier} does not exist",
        failure_code=FailureCode.ENTITY_NOT_FOUND,
        details={"entity_type": kind, "entity_id": str(identifier)},
    )


def _only_organization(state: EnvironmentState) -> Organization:
    for organization in state.organizations.values():
        return organization
    raise _not_found("organization", "*")


def _require_project(state: EnvironmentState, project_id: UUID) -> Project:
    project = state.projects.get(project_id)
    if project is None:
        raise _not_found("project", project_id)
    return project


def _require_task(state: EnvironmentState, task_id: UUID) -> Task:
    task = state.tasks.get(task_id)
    if task is None:
        raise _not_found("task", task_id)
    return task


def _require_team(state: EnvironmentState, team_id: UUID) -> Team:
    team = state.teams.get(team_id)
    if team is None:
        raise _not_found("team", team_id)
    return team


def _require_user(state: EnvironmentState, user_id: UUID) -> EnvironmentUser:
    user = state.users.get(user_id)
    if user is None or not user.is_active:
        raise _not_found("user", user_id)
    return user


def _active_assignments(state: EnvironmentState, task_id: UUID) -> list[TaskAssignment]:
    return [
        assignment
        for assignment in state.task_assignments.values()
        if assignment.task_id == task_id and assignment.removed_at is None
    ]


def _depends_transitively_on(
    state: EnvironmentState, *, start: UUID, target: UUID
) -> bool:
    """True when `start` already depends, directly or not, on `target`."""
    stack = [start]
    seen: set[UUID] = set()
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        for dependency in state.task_dependencies.values():
            if dependency.task_id == current:
                stack.append(dependency.depends_on_task_id)
    return False


# --- handlers ---------------------------------------------------------------


def _create_organization(ctx: ActionContext) -> EnvironmentState:
    params: OrgCreateParams = ctx.params
    require_capability(ctx.state, ctx.actor, Capability.ORG_MANAGE)
    if ctx.state.organizations:
        raise RuleViolation("this environment already has an organization")
    organization = Organization(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        name=params.name,
        slug=params.slug,
        created_at=ctx.now,
    )
    return _touch(_store(ctx.state, "organizations", organization), ctx.now)


def _create_user(ctx: ActionContext) -> EnvironmentState:
    params: UserCreateParams = ctx.params
    require_capability(ctx.state, ctx.actor, Capability.USER_MANAGE)
    organization = _only_organization(ctx.state)
    if params.org_role is OrgRole.OWNER and ctx.actor.org_role is not OrgRole.OWNER:
        raise RuleViolation(
            "only an owner may create another owner",
            failure_code=FailureCode.PERMISSION_DENIED,
        )
    handle = params.handle.lower()
    for existing in ctx.state.users.values():
        if existing.handle.lower() == handle:
            raise RuleViolation(
                f"handle {handle!r} is already taken", details={"handle": handle}
            )
    user = EnvironmentUser(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        org_id=organization.id,
        handle=handle,
        display_name=params.display_name,
        org_role=params.org_role,
        created_at=ctx.now,
    )
    return _touch(_store(ctx.state, "users", user), ctx.now)


def _create_team(ctx: ActionContext) -> EnvironmentState:
    params: TeamCreateParams = ctx.params
    require_capability(ctx.state, ctx.actor, Capability.TEAM_MANAGE)
    organization = _only_organization(ctx.state)
    key = params.key.upper()
    for existing in ctx.state.teams.values():
        if existing.key.upper() == key:
            raise RuleViolation(f"team key {key!r} is already taken")
    team = Team(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        org_id=organization.id,
        key=key,
        name=params.name,
        created_at=ctx.now,
    )
    return _touch(_store(ctx.state, "teams", team), ctx.now)


def _add_team_member(ctx: ActionContext) -> EnvironmentState:
    params: TeamMemberAddParams = ctx.params
    require_capability(ctx.state, ctx.actor, Capability.TEAM_MANAGE)
    team = _require_team(ctx.state, params.team_id)
    user = _require_user(ctx.state, params.user_id)
    for membership in ctx.state.team_memberships.values():
        if (
            membership.team_id == team.id
            and membership.user_id == user.id
            and membership.removed_at is None
        ):
            raise RuleViolation(f"user {user.handle!r} is already on team {team.key!r}")
    membership = TeamMembership(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        team_id=team.id,
        user_id=user.id,
        role=params.role,
        added_by_id=ctx.actor.id,
        added_at=ctx.now,
    )
    return _touch(_store(ctx.state, "team_memberships", membership), ctx.now)


def _create_project(ctx: ActionContext) -> EnvironmentState:
    params: ProjectCreateParams = ctx.params
    require_capability(ctx.state, ctx.actor, Capability.PROJECT_MANAGE)
    organization = _only_organization(ctx.state)
    key = params.key.upper()
    for existing in ctx.state.projects.values():
        if existing.key.upper() == key:
            raise RuleViolation(f"project key {key!r} is already taken")
    project = Project(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        org_id=organization.id,
        key=key,
        name=params.name,
        description=params.description,
        status=ProjectStatus.ACTIVE,
        lead_user_id=None,
        created_by_id=ctx.actor.id,
        started_on=params.started_on,
        target_on=params.target_on,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    stored = _store(ctx.state, "projects", project)
    # The creator is the project's first manager. Without this the project would
    # start with nobody able to take work on it, and "assignee must be a member"
    # would be unsatisfiable for the only actor who can create tasks.
    membership = ProjectMembership(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        project_id=project.id,
        user_id=ctx.actor.id,
        role=ProjectRole.MANAGER,
        added_by_id=ctx.actor.id,
        added_at=ctx.now,
    )
    return _touch(_store(stored, "project_memberships", membership), ctx.now)


def _add_project_member(ctx: ActionContext) -> EnvironmentState:
    params: ProjectMemberAddParams = ctx.params
    project = _require_project(ctx.state, params.project_id)
    require_capability(
        ctx.state, ctx.actor, Capability.PROJECT_MANAGE, project_id=project.id
    )
    user = _require_user(ctx.state, params.user_id)
    rules = ctx.state.environment.config
    if rules.require_project_member_org_membership and user.org_id != project.org_id:
        raise RuleViolation(
            f"user {user.handle!r} is not a member of this organization"
        )
    if active_project_membership(
        ctx.state, project_id=project.id, user_id=user.id
    ) is not None:
        raise RuleViolation(f"user {user.handle!r} is already on project {project.key!r}")
    membership = ProjectMembership(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        project_id=project.id,
        user_id=user.id,
        role=params.role,
        added_by_id=ctx.actor.id,
        added_at=ctx.now,
    )
    return _touch(_store(ctx.state, "project_memberships", membership), ctx.now)


def _create_task(ctx: ActionContext) -> EnvironmentState:
    params: TaskCreateParams = ctx.params
    project = _require_project(ctx.state, params.project_id)
    require_capability(
        ctx.state, ctx.actor, Capability.TASK_EDIT, project_id=project.id
    )
    if project.status in (ProjectStatus.ARCHIVED, ProjectStatus.COMPLETED):
        raise RuleViolation(f"project {project.key!r} no longer accepts tasks")
    rules = ctx.state.environment.config
    if rules.max_tasks_per_project is not None:
        existing = sum(1 for task in ctx.state.tasks.values() if task.project_id == project.id)
        if existing >= rules.max_tasks_per_project:
            raise RuleViolation(
                f"project {project.key!r} already holds {existing} tasks",
                details={"max_tasks_per_project": rules.max_tasks_per_project},
            )
    task = Task(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        project_id=project.id,
        title=params.title,
        description=params.description,
        status=TaskStatus.TODO,
        priority=params.priority,
        task_type=params.task_type,
        created_by_id=ctx.actor.id,
        due_on=params.due_on,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    return _touch(_store(ctx.state, "tasks", task), ctx.now)


def _update_task(ctx: ActionContext) -> EnvironmentState:
    params: TaskUpdateParams = ctx.params
    task = _require_task(ctx.state, params.task_id)
    require_capability(
        ctx.state, ctx.actor, Capability.TASK_EDIT, project_id=task.project_id
    )
    provided = params.model_fields_set - {"task_id"}
    if not provided:
        raise RuleViolation("no fields to update")
    changes = {name: getattr(params, name) for name in provided}
    updated = _with_fields(task, updated_at=ctx.now, **changes)
    return _touch(_store(ctx.state, "tasks", updated), ctx.now)


def _set_task_status(ctx: ActionContext) -> EnvironmentState:
    params: TaskStatusParams = ctx.params
    task = _require_task(ctx.state, params.task_id)
    require_capability(
        ctx.state, ctx.actor, Capability.TASK_TRANSITION, project_id=task.project_id
    )
    if params.status is task.status:
        raise RuleViolation(f"task is already in status {task.status.value!r}")
    rules = ctx.state.environment.config
    if params.status is TaskStatus.DONE and rules.block_completion_with_open_dependencies:
        for dependency in ctx.state.task_dependencies.values():
            if dependency.task_id != task.id:
                continue
            blocker = ctx.state.tasks.get(dependency.depends_on_task_id)
            if blocker is None:
                raise _not_found("task", dependency.depends_on_task_id)
            if blocker.status not in (TaskStatus.DONE, TaskStatus.CANCELLED):
                raise RuleViolation(
                    f"task is blocked by open dependency {blocker.id}",
                    details={
                        "blocking_task_id": str(blocker.id),
                        "blocking_status": blocker.status.value,
                    },
                )
    completed_at: datetime | None
    if params.status is TaskStatus.DONE:
        completed_at = ctx.now
    elif task.status is TaskStatus.DONE:
        completed_at = None
    else:
        completed_at = task.completed_at
    updated = _with_fields(
        task,
        status=params.status,
        completed_at=completed_at,
        updated_at=ctx.now,
    )
    return _touch(_store(ctx.state, "tasks", updated), ctx.now)


def _assign_task(ctx: ActionContext) -> EnvironmentState:
    params: TaskAssignParams = ctx.params
    task = _require_task(ctx.state, params.task_id)
    require_capability(
        ctx.state, ctx.actor, Capability.TASK_EDIT, project_id=task.project_id
    )
    user = _require_user(ctx.state, params.user_id)
    rules = ctx.state.environment.config
    for assignment in _active_assignments(ctx.state, task.id):
        if assignment.user_id == user.id:
            raise RuleViolation(f"user {user.handle!r} is already assigned")
    if len(_active_assignments(ctx.state, task.id)) >= rules.max_assignees_per_task:
        raise RuleViolation(
            f"task already has {rules.max_assignees_per_task} assignee(s)",
            details={"max_assignees_per_task": rules.max_assignees_per_task},
        )
    if rules.require_assignee_project_member and active_project_membership(
        ctx.state, project_id=task.project_id, user_id=user.id
    ) is None:
        raise RuleViolation(
            f"user {user.handle!r} is not a member of this project",
            details={"project_id": str(task.project_id), "user_id": str(user.id)},
        )
    assignment = TaskAssignment(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        task_id=task.id,
        user_id=user.id,
        assigned_by_id=ctx.actor.id,
        assigned_at=ctx.now,
    )
    return _touch(_store(ctx.state, "task_assignments", assignment), ctx.now)


def _unassign_task(ctx: ActionContext) -> EnvironmentState:
    params: TaskUnassignParams = ctx.params
    task = _require_task(ctx.state, params.task_id)
    require_capability(
        ctx.state, ctx.actor, Capability.TASK_EDIT, project_id=task.project_id
    )
    for assignment in _active_assignments(ctx.state, task.id):
        if assignment.user_id != params.user_id:
            continue
        removed = _with_fields(assignment, removed_at=ctx.now)
        return _touch(_store(ctx.state, "task_assignments", removed), ctx.now)
    raise _not_found("assignment", params.user_id)


def _add_dependency(ctx: ActionContext) -> EnvironmentState:
    params: TaskDependencyAddParams = ctx.params
    task = _require_task(ctx.state, params.task_id)
    dependency_task = _require_task(ctx.state, params.depends_on_task_id)
    require_capability(
        ctx.state, ctx.actor, Capability.TASK_EDIT, project_id=task.project_id
    )
    if task.id == dependency_task.id:
        raise RuleViolation("a task cannot depend on itself")
    if task.project_id != dependency_task.project_id:
        raise RuleViolation("both tasks must belong to the same project")
    for existing in ctx.state.task_dependencies.values():
        if (
            existing.task_id == task.id
            and existing.depends_on_task_id == dependency_task.id
        ):
            raise RuleViolation("this dependency already exists")
    rules = ctx.state.environment.config
    if rules.prevent_dependency_cycles and _depends_transitively_on(
        ctx.state, start=dependency_task.id, target=task.id
    ):
        raise RuleViolation(
            "this dependency would create a cycle",
            details={
                "task_id": str(task.id),
                "depends_on_task_id": str(dependency_task.id),
            },
        )
    dependency = TaskDependency(
        id=ctx.ids.new_id(),
        environment_id=ctx.state.environment_id,
        task_id=task.id,
        depends_on_task_id=dependency_task.id,
        created_by_id=ctx.actor.id,
        created_at=ctx.now,
    )
    return _touch(_store(ctx.state, "task_dependencies", dependency), ctx.now)


# --- registry ---------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    params_model: type[BaseModel]
    handler: Callable[[ActionContext], EnvironmentState]
    summary: str


_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        "organization.create",
        OrgCreateParams,
        _create_organization,
        "Create the environment's single organization.",
    ),
    ActionSpec(
        "user.create",
        UserCreateParams,
        _create_user,
        "Add a fictional actor to the environment.",
    ),
    ActionSpec("team.create", TeamCreateParams, _create_team, "Create a team."),
    ActionSpec(
        "team.member.add",
        TeamMemberAddParams,
        _add_team_member,
        "Add an actor to a team.",
    ),
    ActionSpec(
        "project.create", ProjectCreateParams, _create_project, "Create a project."
    ),
    ActionSpec(
        "project.member.add",
        ProjectMemberAddParams,
        _add_project_member,
        "Add an actor to a project.",
    ),
    ActionSpec("task.create", TaskCreateParams, _create_task, "Create a task."),
    ActionSpec(
        "task.update", TaskUpdateParams, _update_task, "Change task attributes."
    ),
    ActionSpec(
        "task.status.set",
        TaskStatusParams,
        _set_task_status,
        "Move a task to another status.",
    ),
    ActionSpec(
        "task.assign", TaskAssignParams, _assign_task, "Assign an actor to a task."
    ),
    ActionSpec(
        "task.unassign",
        TaskUnassignParams,
        _unassign_task,
        "Remove an actor from a task.",
    ),
    ActionSpec(
        "task.dependency.add",
        TaskDependencyAddParams,
        _add_dependency,
        "Declare that one task must finish before another.",
    ),
)

ACTIONS: dict[str, ActionSpec] = {spec.action_type: spec for spec in _SPECS}


def get_action(action_type: str) -> ActionSpec:
    spec = ACTIONS.get(action_type)
    if spec is None:
        raise ValidationError(
            f"unknown action_type {action_type!r}",
            details={"known_action_types": sorted(ACTIONS)},
        )
    return spec


def parse_parameters(spec: ActionSpec, parameters: Any) -> BaseModel:
    """Client input is validated before anything is recorded."""
    try:
        return spec.params_model.model_validate(dict(parameters))
    except PydanticValidationError as exc:
        raise ValidationError(
            f"invalid parameters for {spec.action_type}",
            details={
                "errors": exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            },
        ) from exc
