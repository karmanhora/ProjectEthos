"""HTTP routes. Thin on purpose: translate, call the service, translate back.

No business rule lives here. Every refusal comes from the domain layer, which is
also what writes the failed event, so an HTTP response can never describe an
outcome the log disagrees with.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request

from ethos.api.schemas import (
    ActionRequestModel,
    ActionResponse,
    BootstrapRequest,
    EnvironmentCreated,
    EnvironmentSummary,
    EventSummary,
    IntegrityResponse,
)
from ethos.composition import build_service
from ethos.errors import ErrorCode, EthosError
from ethos.models import EnvironmentEvent
from ethos.runtime import to_plain
from ethos.service import ActionRequest, EnvironmentService
from ethos.state import verify_event_chain

router = APIRouter(prefix="/v1")


def require_api_key(
    request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> None:
    """Optional shared-secret guard. An empty `ETHOS_API_KEY` disables it."""
    expected = request.app.state.settings.api_key
    if not expected:
        return
    if x_api_key != expected:
        raise EthosError(
            "invalid or missing API key",
            code=ErrorCode.UNAUTHORIZED,
            http_status=401,
        )


def _service(request: Request, environment_id: UUID) -> EnvironmentService:
    state = request.app.state
    return build_service(
        state.repository, environment_id, clock=state.clock, ids=state.ids
    )


def _event_summary(event: EnvironmentEvent) -> EventSummary:
    return EventSummary(
        id=event.id,
        sequence=event.sequence,
        action_type=event.action_type,
        actor_user_id=event.actor_user_id,
        origin=event.origin,
        status=event.status,
        failure_code=event.failure_code,
        failure_reason=event.failure_reason,
        resulting_state_version=event.resulting_state_version,
        event_digest=event.event_digest,
        created_at=event.created_at,
    )


@router.post(
    "/environments",
    response_model=EnvironmentCreated,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
def create_environment(
    payload: BootstrapRequest, request: Request
) -> EnvironmentCreated:
    environment_id = uuid4()
    service = _service(request, environment_id)
    result = service.bootstrap(
        name=payload.name,
        description=payload.description,
        org_name=payload.org_name,
        org_slug=payload.org_slug,
        owner_handle=payload.owner_handle,
        owner_display_name=payload.owner_display_name,
    )
    return EnvironmentCreated(
        environment_id=environment_id,
        owner_user_id=next(iter(result.state.users)),
        version=result.state.version,
        last_event_sequence=result.event.sequence,
        last_event_digest=result.event.event_digest,
        event_id=result.event.id,
    )


@router.get(
    "/environments/{environment_id}",
    response_model=EnvironmentSummary,
    dependencies=[Depends(require_api_key)],
)
def read_environment(environment_id: UUID, request: Request) -> EnvironmentSummary:
    service = _service(request, environment_id)
    state = service.repository.load(environment_id)
    head = service.repository.head(environment_id)
    return EnvironmentSummary(
        environment_id=environment_id,
        name=state.environment.name,
        status=state.environment.status,
        version=state.version,
        last_event_sequence=head.last_event_sequence,
        last_event_digest=head.last_event_digest,
        counts={
            "organizations": len(state.organizations),
            "users": len(state.users),
            "teams": len(state.teams),
            "projects": len(state.projects),
            "tasks": len(state.tasks),
            "task_assignments": len(state.task_assignments),
            "task_dependencies": len(state.task_dependencies),
        },
    )


@router.post(
    "/environments/{environment_id}/actions",
    response_model=ActionResponse,
    dependencies=[Depends(require_api_key)],
)
def run_action(
    environment_id: UUID, payload: ActionRequestModel, request: Request
) -> ActionResponse:
    service = _service(request, environment_id)
    result = service.execute(
        ActionRequest(
            action_type=payload.action_type,
            actor_user_id=payload.actor_user_id,
            parameters=payload.parameters,
            idempotency_key=payload.idempotency_key,
            expected_version=payload.expected_version,
        )
    )
    return ActionResponse(
        version=result.state.version,
        replayed=result.replayed,
        event=_event_summary(result.event),
        changed=result.event.changes.changed_entity_ids(),
    )


@router.get(
    "/environments/{environment_id}/events",
    response_model=list[EventSummary],
    dependencies=[Depends(require_api_key)],
)
def list_events(
    environment_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[EventSummary]:
    service = _service(request, environment_id)
    service.repository.load(environment_id)  # surfaces 404 for an unknown environment
    events = service.repository.events(environment_id, after_sequence=after_sequence)
    return [_event_summary(event) for event in events[:limit]]


@router.get(
    "/environments/{environment_id}/integrity",
    response_model=IntegrityResponse,
    dependencies=[Depends(require_api_key)],
)
def check_integrity(
    environment_id: UUID, request: Request, deep: bool = Query(default=True)
) -> IntegrityResponse:
    service = _service(request, environment_id)
    state = service.repository.load(environment_id)
    head = service.repository.head(environment_id)
    report = verify_event_chain(
        service.repository.events(environment_id),
        head_sequence=head.last_event_sequence,
        head_digest=head.last_event_digest,
        current_state=state,
        deep=deep,
    )
    return IntegrityResponse(
        valid=report.valid,
        events_checked=report.events_checked,
        deep=report.deep,
        final_version=report.final_version,
        final_hash=report.final_hash,
        divergence=None if report.divergence is None else to_plain(report.divergence),
    )
