from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    require_json_media_dependency,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.agents import (
    agent_configuration_revision_list_item_resource,
    agent_configuration_revision_resource,
    auth_profile_revision_resource,
)
from atelier2.api.references import PageLimitQuery, RevisionHashQuery
from atelier2.api.wire.requests import (
    PublishAgentConfigurationRevisionRequestResource,
    PublishAuthProfileRevisionRequestResource,
)
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionPageResource,
    AgentConfigurationRevisionResource,
    AuthProfileRevisionPageResource,
    AuthProfileRevisionResource,
)
from atelier2.application.publish_agent_configurations import (
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionPublished,
    AgentConfigurationRevisionUnchanged,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionNotFound,
    AuthProfileRevisionPublished,
    AuthProfileRevisionUnchanged,
    UnpublishableAgentConfiguration,
    UnpublishableAuthProfile,
)
from atelier2.application.read_agent_configurations import (
    AgentConfigurationRevisionsListed,
    AuthProfileRevisionsListed,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevisionHash,
    AuthProfileRevisionHash,
)
from atelier2.contracts.pages import DEFAULT_PAGE_LIMIT

router = APIRouter()


@router.post(
    API_PREFIX + "/auth-profile-revisions",
    response_model=AuthProfileRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AuthProfileRevisionResource}},
)
async def publish_auth_profile_revision_route(
    body: PublishAuthProfileRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_auth_profile_revision(
            body.profile_id, body.revision_number, body.provider_id, body.auth_mode
        ),
    )
    match result:
        case AuthProfileRevisionPublished(stored):
            status = HTTPStatus.CREATED
        case AuthProfileRevisionUnchanged(stored):
            status = HTTPStatus.OK
        case UnpublishableAuthProfile():
            raise ApiProblem("invalid-request")
        case AuthProfileRevisionConflict():
            raise ApiProblem("auth-profile-revision-conflict")
        case AuthProfileRevisionCollision():
            raise ApiProblem("auth-profile-revision-collision")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(auth_profile_revision_resource(stored), status)


@router.get(
    API_PREFIX + "/auth-profile-revisions",
    response_model=AuthProfileRevisionPageResource,
)
async def list_auth_profile_revisions_route(
    after_revision_hash: RevisionHashQuery | None = None,
    limit: PageLimitQuery = DEFAULT_PAGE_LIMIT,
    context: ApiContext = api_context_dependency,
) -> AuthProfileRevisionPageResource:
    after = None
    if after_revision_hash is not None:
        try:
            after = AuthProfileRevisionHash(after_revision_hash)
        except ValueError as error:
            raise ApiProblem("invalid-revision-hash") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_auth_profile_revisions(after, limit),
    )
    match result:
        case AuthProfileRevisionsListed(items, next_after):
            return AuthProfileRevisionPageResource(
                items=tuple(
                    auth_profile_revision_resource(revision) for revision in items
                ),
                next_after_revision_hash=(
                    None if next_after is None else next_after.value
                ),
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.post(
    API_PREFIX + "/agent-configuration-revisions",
    response_model=AgentConfigurationRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AgentConfigurationRevisionResource}},
)
async def publish_agent_configuration_revision_route(
    body: PublishAgentConfigurationRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_agent_configuration_revision(
            body.model,
            body.auth_profile_revision_hash,
            body.executor_revision,
            body.requested_capability,
        ),
    )
    match result:
        case AgentConfigurationRevisionPublished(stored, auth_profile):
            status = HTTPStatus.CREATED
        case AgentConfigurationRevisionUnchanged(stored, auth_profile):
            status = HTTPStatus.OK
        case UnpublishableAgentConfiguration():
            raise ApiProblem("invalid-request")
        case AuthProfileRevisionNotFound():
            raise ApiProblem("auth-profile-revision-not-found")
        case AgentExecutorBindingUnavailable():
            raise ApiProblem("agent-executor-binding-unavailable")
        case AgentConfigurationRevisionCollision():
            raise ApiProblem("agent-configuration-revision-collision")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        agent_configuration_revision_resource(stored, auth_profile), status
    )


@router.get(
    API_PREFIX + "/agent-configuration-revisions",
    response_model=AgentConfigurationRevisionPageResource,
)
async def list_agent_configuration_revisions_route(
    after_revision_hash: RevisionHashQuery | None = None,
    limit: PageLimitQuery = DEFAULT_PAGE_LIMIT,
    context: ApiContext = api_context_dependency,
) -> AgentConfigurationRevisionPageResource:
    after = None
    if after_revision_hash is not None:
        try:
            after = AgentConfigurationRevisionHash(after_revision_hash)
        except ValueError as error:
            raise ApiProblem("invalid-revision-hash") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_agent_configuration_revisions(after, limit),
    )
    match result:
        case AgentConfigurationRevisionsListed(items, next_after):
            return AgentConfigurationRevisionPageResource(
                items=tuple(
                    agent_configuration_revision_list_item_resource(item)
                    for item in items
                ),
                next_after_revision_hash=(
                    None if next_after is None else next_after.value
                ),
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
