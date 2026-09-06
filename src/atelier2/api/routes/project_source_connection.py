from __future__ import annotations

from typing import assert_never

from fastapi import APIRouter, Response, status

from atelier2.api._support import (
    decode_public_project_reference_value,
    decode_public_source_reference_value,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import (
    PROJECT_SOURCE_CONNECTION_PATH,
    PROJECT_SOURCE_PATH,
    PROJECT_SOURCE_TOKEN_PATH,
    PROJECT_SOURCES_PATH,
)
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.project_source_connection import (
    project_source_connection_revision_resource,
    project_source_resource,
)
from atelier2.api.references import (
    PublicProjectReferencePathParameter,
    PublicSourceReferencePathParameter,
)
from atelier2.api.wire.requests import (
    ConnectProjectSourceRequestResource,
    RotateProjectSourceTokenRequestResource,
)
from atelier2.api.wire.resources import (
    ProjectSourceConnectionRevisionResource,
    ProjectSourceListResource,
    ProjectSourceResource,
)
from atelier2.application.project_connections import (
    ConnectionProjectUnknown,
    ManagedProjectSourcePublished,
    PlatformConnectionUnknown,
    ProjectSourceAlreadyConnected,
    ProjectSourceConnectionRead,
    ProjectSourceDisconnected,
    ProjectSourceDisconnectedSuccessfully,
    ProjectSourceInvalid,
    ProjectSourcesRead,
    ProjectSourceTokenRefused,
    ProjectSourceUnavailable,
    ProjectSourceUnknown,
)
from atelier2.application.read_projects import ServedProjectUnknown
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)

router = APIRouter()


@router.get(
    PROJECT_SOURCE_CONNECTION_PATH,
    response_model=ProjectSourceConnectionRevisionResource,
)
async def get_project_source_connection_route(
    public_project_reference: PublicProjectReferencePathParameter,
    context: ApiContext = api_context_dependency,
) -> ProjectSourceConnectionRevisionResource:
    project_id = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_project_source_connection(project_id),
    )
    match result:
        case ProjectSourceConnectionRead(revision, public_address):
            return project_source_connection_revision_resource(revision, public_address)
        case ServedProjectUnknown():
            raise ApiProblem("project-unknown")
        case PlatformConnectionUnknown():
            raise ApiProblem("project-source-not-connected")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def _source_problem(result: object) -> None:
    match result:
        case ConnectionProjectUnknown() | ServedProjectUnknown():
            raise ApiProblem("project-unknown")
        case ProjectSourceUnknown():
            raise ApiProblem("project-source-unknown")
        case ProjectSourceDisconnected():
            raise ApiProblem("project-source-disconnected")
        case ProjectSourceInvalid():
            raise ApiProblem("project-source-invalid")
        case ProjectSourceTokenRefused():
            raise ApiProblem("project-source-token-refused")
        case ProjectSourceUnavailable() | WriteUnavailable():
            raise ApiProblem("project-source-unavailable")
        case ReadUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _:
            raise ApiProblem("internal-error")


@router.get(PROJECT_SOURCES_PATH, response_model=ProjectSourceListResource)
async def list_project_sources_route(
    public_project_reference: PublicProjectReferencePathParameter,
    context: ApiContext = api_context_dependency,
) -> ProjectSourceListResource:
    project_id = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_project_sources(project_id),
    )
    match result:
        case ProjectSourcesRead(sources):
            return ProjectSourceListResource(
                items=tuple(project_source_resource(source) for source in sources)
            )
        case _:
            _source_problem(result)
            raise ApiProblem("internal-error")


@router.post(
    PROJECT_SOURCES_PATH,
    response_model=ProjectSourceResource,
    status_code=status.HTTP_201_CREATED,
)
async def connect_project_source_route(
    request: ConnectProjectSourceRequestResource,
    public_project_reference: PublicProjectReferencePathParameter,
    context: ApiContext = api_context_dependency,
) -> ProjectSourceResource:
    project_id = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    token = request.token.get_secret_value()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.connect_project_source(
            project_id, request.address, token
        ),
    )
    match result:
        case ManagedProjectSourcePublished(source=source):
            return project_source_resource(source)
        case ProjectSourceAlreadyConnected():
            raise ApiProblem("project-source-already-connected")
        case _:
            _source_problem(result)
            raise ApiProblem("internal-error")


@router.delete(PROJECT_SOURCE_PATH, status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_project_source_route(
    public_project_reference: PublicProjectReferencePathParameter,
    public_source_reference: PublicSourceReferencePathParameter,
    context: ApiContext = api_context_dependency,
) -> Response:
    project_id = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    source_id = decode_public_source_reference_value(
        public_source_reference, context.limits
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.disconnect_project_source(project_id, source_id),
    )
    match result:
        case ProjectSourceDisconnectedSuccessfully():
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        case _:
            _source_problem(result)
            raise ApiProblem("internal-error")


@router.put(PROJECT_SOURCE_TOKEN_PATH, response_model=ProjectSourceResource)
async def rotate_project_source_token_route(
    request: RotateProjectSourceTokenRequestResource,
    public_project_reference: PublicProjectReferencePathParameter,
    public_source_reference: PublicSourceReferencePathParameter,
    context: ApiContext = api_context_dependency,
) -> ProjectSourceResource:
    project_id = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    source_id = decode_public_source_reference_value(
        public_source_reference, context.limits
    )
    token = request.token.get_secret_value()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.rotate_project_source_token(
            project_id, source_id, token
        ),
    )
    match result:
        case ManagedProjectSourcePublished(source=source):
            return project_source_resource(source)
        case _:
            _source_problem(result)
            raise ApiProblem("internal-error")
