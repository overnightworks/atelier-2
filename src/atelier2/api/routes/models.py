from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, assert_never

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    decode_public_project_reference_value,
    require_json_media_dependency,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import (
    MODEL_REGISTRY_PATH,
    MODEL_REGISTRY_VALIDATIONS_PATH,
    PROJECT_MODEL_DEFAULTS_PATH,
    PROJECT_MODEL_RESOLUTION_PATH,
)
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.models import (
    model_registry_resource,
    project_model_defaults_resource,
    project_model_resolution_resource,
)
from atelier2.api.references import PublicProjectReferencePath
from atelier2.api.wire.requests import (
    PutModelRegistryRevisionRequestResource,
    PutProjectModelDefaultsRevisionRequestResource,
    ResolveProjectModelsRequestResource,
    ValidateModelRegistryEntryRequestResource,
)
from atelier2.api.wire.resources import (
    ModelRegistryRevisionResource,
    ProjectModelDefaultsRevisionResource,
    ProjectModelResolutionResource,
)
from atelier2.application.model_configuration import (
    ModelConfigurationProjectUnknown,
    ModelRegistryCollision,
    ModelRegistryConflict,
    ModelRegistryInvalid,
    ModelRegistryMissing,
    ModelRegistryPublished,
    ModelRegistryRead,
    ModelRegistryUnchanged,
    ModelResolutionInvalidAgentBindings,
    ModelResolutionWorkflowMissing,
    ModelResolutionWorkflowUnsupported,
    ProjectModelDefaultsCollision,
    ProjectModelDefaultsConflict,
    ProjectModelDefaultsInvalid,
    ProjectModelDefaultsMissing,
    ProjectModelDefaultsPublished,
    ProjectModelDefaultsRead,
    ProjectModelDefaultsUnchanged,
    ProjectModelResolutionRead,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.agents import (
    MAXIMUM_PROVIDER_ID_CHARACTERS,
    PROVIDER_ID_PATTERN,
)
from atelier2.contracts.host_configuration import (
    MODEL_REGISTRY_REVISION_CONFLICT,
    PROJECT_MODEL_DEFAULTS_REVISION_CONFLICT,
    ProjectId,
)
from atelier2.contracts.runs import WorkflowRevisionHash

router = APIRouter()


def _project_id(public_reference: str, context: ApiContext) -> ProjectId:
    return decode_public_project_reference_value(public_reference, context.limits)


@router.put(
    MODEL_REGISTRY_PATH,
    response_model=ModelRegistryRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ModelRegistryRevisionResource}},
)
async def put_model_registry_route(
    provider_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=MAXIMUM_PROVIDER_ID_CHARACTERS,
            pattern=PROVIDER_ID_PATTERN,
        ),
    ],
    body: PutModelRegistryRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: Annotated[None, Depends(require_json_media_dependency)] = None,
) -> JSONResponse:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_model_registry(
            provider_id,
            body.revision_number,
            tuple(
                (
                    entry.model_id,
                    entry.agent_configuration_revision_hash,
                )
                for entry in body.entries
            ),
        ),
    )
    match result:
        case ModelRegistryPublished(revision):
            status = HTTPStatus.CREATED
        case ModelRegistryUnchanged(revision):
            status = HTTPStatus.OK
        case ModelRegistryInvalid():
            raise ApiProblem("invalid-request")
        case ModelRegistryConflict():
            raise ApiProblem(MODEL_REGISTRY_REVISION_CONFLICT)
        case ModelRegistryCollision():
            raise ApiProblem("model-registry-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(model_registry_resource(revision), status)


@router.post(
    MODEL_REGISTRY_VALIDATIONS_PATH,
    response_model=ModelRegistryRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ModelRegistryRevisionResource}},
)
async def validate_model_registry_entry_route(
    provider_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=MAXIMUM_PROVIDER_ID_CHARACTERS,
            pattern=PROVIDER_ID_PATTERN,
        ),
    ],
    body: ValidateModelRegistryEntryRequestResource,
    context: ApiContext = api_context_dependency,
    _media: Annotated[None, Depends(require_json_media_dependency)] = None,
) -> JSONResponse:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.validate_model_registry_entry(
            provider_id, body.agent_configuration_revision_hash
        ),
    )
    match result:
        case ModelRegistryPublished(revision):
            status = HTTPStatus.CREATED
        case ModelRegistryUnchanged(revision):
            status = HTTPStatus.OK
        case ModelRegistryInvalid():
            raise ApiProblem("invalid-request")
        case ModelRegistryConflict():
            raise ApiProblem(MODEL_REGISTRY_REVISION_CONFLICT)
        case ModelRegistryCollision():
            raise ApiProblem("model-registry-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(model_registry_resource(revision), status)


@router.get(MODEL_REGISTRY_PATH)
async def get_model_registry_route(
    provider_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=MAXIMUM_PROVIDER_ID_CHARACTERS,
            pattern=PROVIDER_ID_PATTERN,
        ),
    ],
    context: ApiContext = api_context_dependency,
) -> ModelRegistryRevisionResource:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_model_registry(provider_id),
    )
    match result:
        case ModelRegistryRead(revision):
            return model_registry_resource(revision)
        case ModelRegistryMissing():
            raise ApiProblem("model-registry-missing")
        case ModelRegistryInvalid():
            raise ApiProblem("invalid-request")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.put(
    PROJECT_MODEL_DEFAULTS_PATH,
    response_model=ProjectModelDefaultsRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ProjectModelDefaultsRevisionResource}},
)
async def put_project_model_defaults_route(
    public_project_reference: PublicProjectReferencePath,
    body: PutProjectModelDefaultsRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: Annotated[None, Depends(require_json_media_dependency)] = None,
) -> JSONResponse:
    project = _project_id(public_project_reference, context)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_project_model_defaults(
            project.value,
            body.revision_number,
            tuple(
                (
                    default.difficulty,
                    default.model_registry_revision_hash,
                    default.provider_id,
                    default.model_id,
                    default.agent_configuration_revision_hash,
                )
                for default in body.defaults
            ),
        ),
    )
    match result:
        case ProjectModelDefaultsPublished(revision):
            status = HTTPStatus.CREATED
        case ProjectModelDefaultsUnchanged(revision):
            status = HTTPStatus.OK
        case ModelConfigurationProjectUnknown():
            raise ApiProblem("project-unknown")
        case ProjectModelDefaultsInvalid():
            raise ApiProblem("invalid-request")
        case ProjectModelDefaultsConflict():
            raise ApiProblem(PROJECT_MODEL_DEFAULTS_REVISION_CONFLICT)
        case ProjectModelDefaultsCollision():
            raise ApiProblem("project-model-defaults-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(project_model_defaults_resource(revision), status)


@router.get(PROJECT_MODEL_DEFAULTS_PATH)
async def get_project_model_defaults_route(
    public_project_reference: PublicProjectReferencePath,
    context: ApiContext = api_context_dependency,
) -> ProjectModelDefaultsRevisionResource:
    project = _project_id(public_project_reference, context)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_project_model_defaults(project.value),
    )
    match result:
        case ProjectModelDefaultsRead(revision):
            return project_model_defaults_resource(revision)
        case ProjectModelDefaultsMissing():
            raise ApiProblem("project-model-defaults-missing")
        case ModelConfigurationProjectUnknown():
            raise ApiProblem("project-unknown")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.post(PROJECT_MODEL_RESOLUTION_PATH)
async def resolve_project_models_route(
    public_project_reference: PublicProjectReferencePath,
    body: ResolveProjectModelsRequestResource,
    context: ApiContext = api_context_dependency,
    _media: Annotated[None, Depends(require_json_media_dependency)] = None,
) -> ProjectModelResolutionResource:
    project = _project_id(public_project_reference, context)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_project_model_resolution(
            project.value,
            body.workflow_revision_hash,
            tuple(
                (override.role, override.agent_configuration_revision_hash)
                for override in body.overrides
            ),
        ),
    )
    match result:
        case ProjectModelResolutionRead(resolution):
            return project_model_resolution_resource(
                project, WorkflowRevisionHash(body.workflow_revision_hash), resolution
            )
        case ModelConfigurationProjectUnknown():
            raise ApiProblem("project-unknown")
        case ModelResolutionWorkflowMissing():
            raise ApiProblem("workflow-revision-not-found")
        case ModelResolutionWorkflowUnsupported():
            raise ApiProblem("workflow-format-not-executable")
        case ModelResolutionInvalidAgentBindings():
            raise ApiProblem("invalid-agent-bindings")
        case ModelRegistryInvalid():
            raise ApiProblem("invalid-request")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
