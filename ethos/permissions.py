"""Capability model: which actor may perform which action.

Roles are translated into capabilities exactly once, here. Handlers ask for a
capability and never for a role, so editing the role table cannot silently widen
what an individual handler allows.

A denial is raised as `RuleViolation` with `FailureCode.PERMISSION_DENIED`
rather than as `PermissionDenied`, because a refused action is still a fact
about the world and is recorded as a failed event.
"""
from __future__ import annotations

from enum import Enum
from uuid import UUID

from ethos.errors import FailureCode, RuleViolation
from ethos.models import (
    EnvironmentState,
    EnvironmentUser,
    OrgRole,
    ProjectMembership,
    ProjectRole,
)

__all__ = [
    "Capability",
    "ORG_CAPABILITIES",
    "PROJECT_CAPABILITIES",
    "active_project_membership",
    "capabilities_for",
    "require_capability",
]


class Capability(str, Enum):
    ORG_MANAGE = "org.manage"
    USER_MANAGE = "user.manage"
    TEAM_MANAGE = "team.manage"
    PROJECT_MANAGE = "project.manage"
    TASK_EDIT = "task.edit"
    TASK_TRANSITION = "task.transition"
    VIEW = "view"


_ORG_FULL = frozenset(
    {
        Capability.ORG_MANAGE,
        Capability.USER_MANAGE,
        Capability.TEAM_MANAGE,
        Capability.PROJECT_MANAGE,
        Capability.TASK_EDIT,
        Capability.TASK_TRANSITION,
        Capability.VIEW,
    }
)

ORG_CAPABILITIES: dict[OrgRole, frozenset[Capability]] = {
    OrgRole.OWNER: _ORG_FULL,
    OrgRole.ADMIN: _ORG_FULL,
    OrgRole.MEMBER: frozenset(
        {Capability.TASK_EDIT, Capability.TASK_TRANSITION, Capability.VIEW}
    ),
    OrgRole.VIEWER: frozenset({Capability.VIEW}),
}

PROJECT_CAPABILITIES: dict[ProjectRole, frozenset[Capability]] = {
    ProjectRole.MANAGER: frozenset(
        {
            Capability.PROJECT_MANAGE,
            Capability.TASK_EDIT,
            Capability.TASK_TRANSITION,
            Capability.VIEW,
        }
    ),
    ProjectRole.CONTRIBUTOR: frozenset(
        {Capability.TASK_EDIT, Capability.TASK_TRANSITION, Capability.VIEW}
    ),
    ProjectRole.VIEWER: frozenset({Capability.VIEW}),
}


def active_project_membership(
    state: EnvironmentState, *, project_id: UUID, user_id: UUID
) -> ProjectMembership | None:
    for membership in state.project_memberships.values():
        if (
            membership.project_id == project_id
            and membership.user_id == user_id
            and membership.removed_at is None
        ):
            return membership
    return None


def capabilities_for(
    state: EnvironmentState,
    user: EnvironmentUser,
    *,
    project_id: UUID | None = None,
) -> frozenset[Capability]:
    """Org role first, then the project role layered on top."""
    if not user.is_active:
        return frozenset()
    capabilities = set(ORG_CAPABILITIES[user.org_role])
    if project_id is not None:
        membership = active_project_membership(
            state, project_id=project_id, user_id=user.id
        )
        if membership is not None:
            capabilities |= PROJECT_CAPABILITIES[membership.role]
    return frozenset(capabilities)


def require_capability(
    state: EnvironmentState,
    user: EnvironmentUser,
    capability: Capability,
    *,
    project_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    if capability in capabilities_for(state, user, project_id=project_id):
        return
    details: dict[str, str] = {
        "capability": capability.value,
        "user_id": str(user.id),
    }
    if project_id is not None:
        details["project_id"] = str(project_id)
    raise RuleViolation(
        reason or f"actor {user.handle!r} lacks {capability.value}",
        failure_code=FailureCode.PERMISSION_DENIED,
        details=details,
    )
