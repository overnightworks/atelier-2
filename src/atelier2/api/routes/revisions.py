from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from atelier2.api._support import (
    parse_revision_view,
    require_media_type,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.limits import ApiLimitExceeded
from atelier2.api.openapi import (
    API_PREFIX,
    CATALOG_LINEAGES_PATH,
    CATALOG_REVISION_BY_NAME_PATH,
    LIBRARY_ADDITION_PATH,
    LIBRARY_ADDITIONS_PATH,
    LIBRARY_RECOGNITIONS_PATH,
)
from atelier2.api.problems import (
    ApiProblem,
    adapter_operation_document_problem_code,
    agent_definition_document_problem_code,
    budget_document_problem_code,
    schema_document_problem_code,
    tool_grant_document_problem_code,
)
from atelier2.api.projection.workflows import (
    workflow_revision_detail_resource,
    workflow_revision_page_resource,
)
from atelier2.api.references import (
    PageLimitQuery,
    RevisionHashPath,
    RevisionHashQuery,
    parse_revision_hash,
)
from atelier2.api.routes.catalog_lineage import catalog_admission_resource
from atelier2.api.wire.library import (
    DocumentNotHeldResource,
    DocumentUnrecognizedResource,
    KindRefusalResource,
    LibraryAdditionResource,
    LibraryRecognitionResource,
    RecognizedAgentDefinitionResource,
    RecognizedWorkflowResource,
)
from atelier2.api.wire.requests import (
    FoundCatalogLineageRequestResource,
    RevisionListingView,
)
from atelier2.api.wire.resources import (
    AdapterOperationRevisionResource,
    AgentDefinitionRevisionDetailResource,
    AgentDefinitionRevisionListItemResource,
    AgentDefinitionRevisionPageResource,
    AgentDefinitionRevisionResource,
    AnyWorkflowRevisionPageResource,
    BudgetRevisionResource,
    CatalogAdmissionResource,
    CatalogNameResolutionResource,
    SchemaRevisionResource,
    ToolGrantRevisionResource,
    VersionedWorkflowRevisionPageResource,
    WorkflowRevisionDetailResource,
    WorkflowRevisionPageResource,
    WorkflowRevisionSummaryResource,
)
from atelier2.application.admit_library_addition import (
    LibraryAdditionExisting,
    LibraryAdditionFound,
    LibraryAdditionMissing,
    LibraryAdditionStored,
)
from atelier2.application.classify_definition_document import (
    ClassifyDefinitionDocumentResult,
)
from atelier2.application.publish_adapter_operation_revision import (
    AdapterOperationPublicationCollision,
    AdapterOperationPublicationCreated,
    AdapterOperationPublicationExisting,
    AdapterOperationPublicationInvalid,
)
from atelier2.application.publish_agent_definition_revision import (
    AgentDefinitionPublicationCollision,
    AgentDefinitionPublicationCreated,
    AgentDefinitionPublicationExisting,
    AgentDefinitionPublicationInvalid,
)
from atelier2.application.publish_budget_revision import (
    BudgetPublicationCollision,
    BudgetPublicationCreated,
    BudgetPublicationExisting,
    BudgetPublicationInvalid,
)
from atelier2.application.publish_schema_revision import (
    SchemaPublicationCollision,
    SchemaPublicationCreated,
    SchemaPublicationExisting,
    SchemaPublicationInvalid,
    SchemaRevisionNotFound,
    SchemaRevisionRead,
)
from atelier2.application.publish_tool_grant_revision import (
    ToolGrantPublicationCollision,
    ToolGrantPublicationCreated,
    ToolGrantPublicationExisting,
    ToolGrantPublicationInvalid,
)
from atelier2.application.publish_workflow_revision import (
    PublicationCollision,
    PublicationCreated,
    PublicationExisting,
    PublicationInvalid,
)
from atelier2.application.read_agent_definition_revisions import (
    AgentDefinitionRevisionNotFound,
    AgentDefinitionRevisionRead,
    AgentDefinitionRevisionsListed,
    PublishedAgentDefinition,
)
from atelier2.application.read_workflow_revisions import (
    WorkflowRevisionNotFound,
    WorkflowRevisionRead,
    WorkflowRevisionsDescribed,
    WorkflowRevisionsListed,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.application.resolve_catalog_name import (
    CatalogNameInvalidPosition,
    CatalogNameLineageRetired,
    CatalogNameMissing,
    CatalogNameResolved,
    CatalogReferenceNonMember,
)
from atelier2.contracts.agent_definitions import UnrestrictedTools
from atelier2.contracts.catalog_intakes import CatalogIntakeId, CatalogIntakeKind
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    catalog_lineage_query,
)
from atelier2.contracts.library_recognition import (
    DocumentAmbiguous,
    DocumentNotHeld,
    DocumentUnrecognized,
    LibraryDocumentKind,
    RecognizedAgentDefinition,
    RecognizedWorkflow,
)
from atelier2.contracts.pages import DEFAULT_PAGE_LIMIT
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_refusals import WorkflowRefusal

_APPLICATION_JSON = "application/json"
router = APIRouter()


@router.post(
    API_PREFIX + "/schema-revisions",
    response_model=SchemaRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": SchemaRevisionResource}},
)
async def publish_schema_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, _APPLICATION_JSON)
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_schema_revision(document),
    )
    match result:
        case SchemaPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case SchemaPublicationExisting(revision):
            status = HTTPStatus.OK
        case SchemaPublicationInvalid(verdict):
            raise ApiProblem(
                schema_document_problem_code(verdict.refusal), str(verdict)
            )
        case SchemaPublicationCollision():
            raise ApiProblem("schema-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        SchemaRevisionResource(schema_revision_hash=revision.revision_hash.value),
        status,
    )


def _revision_hash_or_refuse[T](value: str, parse: Callable[[str], T]) -> T:
    try:
        return parse(value)
    except ValueError as error:
        raise ApiProblem("invalid-revision-hash") from error


@router.get(
    API_PREFIX + "/schema-revisions/{schema_revision_hash}",
    responses={
        HTTPStatus.OK: {
            "content": {
                _APPLICATION_JSON: {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def get_schema_revision_route(
    schema_revision_hash: RevisionHashPath, context: ApiContext = api_context_dependency
) -> Response:
    """The exact bytes a `schema` reference pins, for a caller holding only the hash.

    A published `schema` revision is named by hash alone (ADR 0007), so this
    mirrors the byte-in door it answers: `application/json`, the same media
    type `POST /schema-revisions` accepted the document as, verbatim.
    """
    parsed = _revision_hash_or_refuse(schema_revision_hash, PublishedRevisionHash)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_schema_revision(parsed),
    )
    match result:
        case SchemaRevisionRead(revision):
            return Response(revision.document, media_type=_APPLICATION_JSON)
        case SchemaRevisionNotFound():
            raise ApiProblem("schema-revision-not-found")
        case _ as unreachable:
            assert_never(unreachable)


@router.post(
    API_PREFIX + "/budget-revisions",
    response_model=BudgetRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": BudgetRevisionResource}},
)
async def publish_budget_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, _APPLICATION_JSON)
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_budget_revision(document),
    )
    match result:
        case BudgetPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case BudgetPublicationExisting(revision):
            status = HTTPStatus.OK
        case BudgetPublicationInvalid(verdict):
            raise ApiProblem(budget_document_problem_code(verdict.reason), str(verdict))
        case BudgetPublicationCollision():
            raise ApiProblem("budget-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        BudgetRevisionResource(budget_revision_hash=revision.revision_hash.value),
        status,
    )


@router.post(
    API_PREFIX + "/tool-grant-revisions",
    response_model=ToolGrantRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ToolGrantRevisionResource}},
)
async def publish_tool_grant_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, _APPLICATION_JSON)
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_tool_grant_revision(document),
    )
    match result:
        case ToolGrantPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case ToolGrantPublicationExisting(revision):
            status = HTTPStatus.OK
        case ToolGrantPublicationInvalid(verdict):
            raise ApiProblem(
                tool_grant_document_problem_code(verdict.reason), str(verdict)
            )
        case ToolGrantPublicationCollision():
            raise ApiProblem("tool-grant-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        ToolGrantRevisionResource(
            tool_grant_revision_hash=revision.revision_hash.value
        ),
        status,
    )


@router.post(
    API_PREFIX + "/adapter-operation-revisions",
    response_model=AdapterOperationRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AdapterOperationRevisionResource}},
)
async def publish_adapter_operation_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, _APPLICATION_JSON)
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_adapter_operation_revision(document),
    )
    match result:
        case AdapterOperationPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case AdapterOperationPublicationExisting(revision):
            status = HTTPStatus.OK
        case AdapterOperationPublicationInvalid(verdict):
            raise ApiProblem(
                adapter_operation_document_problem_code(verdict.reason), str(verdict)
            )
        case AdapterOperationPublicationCollision():
            raise ApiProblem("adapter-operation-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        AdapterOperationRevisionResource(
            adapter_operation_revision_hash=revision.revision_hash.value
        ),
        status,
    )


@router.post(
    API_PREFIX + "/agent-definition-revisions",
    response_model=AgentDefinitionRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AgentDefinitionRevisionResource}},
)
async def publish_agent_definition_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, "text/markdown")
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_agent_definition_revision(document),
    )
    match result:
        case AgentDefinitionPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case AgentDefinitionPublicationExisting(revision):
            status = HTTPStatus.OK
        case AgentDefinitionPublicationInvalid(verdict):
            raise ApiProblem(
                agent_definition_document_problem_code(verdict.refusal), str(verdict)
            )
        case AgentDefinitionPublicationCollision():
            raise ApiProblem("agent-definition-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        AgentDefinitionRevisionResource(
            agent_definition_revision_hash=revision.revision_hash.value
        ),
        status,
    )


@router.get(
    API_PREFIX + "/agent-definition-revisions",
    response_model=AgentDefinitionRevisionPageResource,
)
async def list_agent_definition_revisions_route(
    after_revision_hash: RevisionHashQuery | None = None,
    limit: PageLimitQuery = DEFAULT_PAGE_LIMIT,
    context: ApiContext = api_context_dependency,
) -> AgentDefinitionRevisionPageResource:
    """List published agent definitions by the names their authors gave them."""

    after = None
    if after_revision_hash is not None:
        after = _revision_hash_or_refuse(after_revision_hash, PublishedRevisionHash)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_agent_definition_revisions(after, limit),
    )
    match result:
        case AgentDefinitionRevisionsListed(items, next_after):
            return AgentDefinitionRevisionPageResource(
                items=tuple(_agent_definition_list_item(item) for item in items),
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


def _agent_definition_list_item(
    published: PublishedAgentDefinition,
) -> AgentDefinitionRevisionListItemResource:
    return AgentDefinitionRevisionListItemResource(
        agent_definition_revision_hash=published.revision_hash.value,
        name=published.definition.name,
        description=published.definition.description,
    )


@router.get(
    API_PREFIX + "/agent-definition-revisions/{agent_definition_revision_hash}",
    response_model=AgentDefinitionRevisionDetailResource,
)
async def get_agent_definition_revision_route(
    agent_definition_revision_hash: RevisionHashPath,
    context: ApiContext = api_context_dependency,
) -> AgentDefinitionRevisionDetailResource:
    """The whole authored definition a caller holding its hash asked to read.

    Mirrors `GET /schema-revisions/{hash}`: named by hash alone (ADR 0007), so
    this answers nothing a reference did not already carry -- parsed back into
    the definition its author wrote, exactly as the publish door parsed it once
    already.
    """
    parsed = _revision_hash_or_refuse(
        agent_definition_revision_hash, PublishedRevisionHash
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_agent_definition_revision(parsed),
    )
    match result:
        case AgentDefinitionRevisionRead(published):
            return _agent_definition_detail_resource(published)
        case AgentDefinitionRevisionNotFound():
            raise ApiProblem("agent-definition-revision-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def _agent_definition_detail_resource(
    published: PublishedAgentDefinition,
) -> AgentDefinitionRevisionDetailResource:
    definition = published.definition
    return AgentDefinitionRevisionDetailResource(
        agent_definition_revision_hash=published.revision_hash.value,
        name=definition.name,
        description=definition.description,
        model=definition.model,
        system_prompt=definition.system_prompt,
        tools=(
            None
            if isinstance(definition.tools, UnrestrictedTools)
            else tuple(name.value for name in definition.tools.names)
        ),
    )


@router.post(
    LIBRARY_RECOGNITIONS_PATH,
    response_model=LibraryRecognitionResource,
    status_code=HTTPStatus.OK,
)
async def recognize_library_document_route(
    request: Request,
    file_name: str | None = None,
    context: ApiContext = api_context_dependency,
) -> JSONResponse:
    """Say what a loose document is, and write nothing.

    The body is opaque bytes because the caller does not know the kind -- that
    is the question. The file name travels beside it because ADR 0018's
    selection grammar lets a name decide what identical frontmatter cannot.
    """
    require_media_type(request, "application/octet-stream")
    if file_name is not None:
        try:
            context.limits.require_field(file_name)
        except ApiLimitExceeded as error:
            raise ApiProblem("invalid-request") from error
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.classify_definition_document(document, file_name),
    )
    return resource_response(_recognition_resource(result), HTTPStatus.OK)


def _recognition_resource(result: ClassifyDefinitionDocumentResult) -> BaseModel:
    match result:
        case RecognizedWorkflow(format_version, name, description):
            return RecognizedWorkflowResource(
                outcome="workflow",
                workflow_format_version=format_version.value,
                name=name,
                description=description,
            )
        case RecognizedAgentDefinition(name, description, provider):
            return RecognizedAgentDefinitionResource(
                outcome="agent_definition",
                name=name,
                description=description,
                provider_id=provider.value,
            )
        case DocumentNotHeld(kind, reason):
            return DocumentNotHeldResource(
                outcome="not_held", kind=kind.value, reason=reason
            )
        case DocumentUnrecognized(refusals):
            return DocumentUnrecognizedResource(
                outcome="unrecognized",
                refusals=tuple(
                    KindRefusalResource(
                        kind=refusal.kind.value,
                        expected=refusal.expected,
                        refused_because=refusal.refused_because,
                    )
                    for refusal in refusals
                ),
            )
        case DocumentAmbiguous(kinds):
            raise ApiProblem("library-document-ambiguous", _ambiguous_detail(kinds))
        case _ as unreachable:
            assert_never(unreachable)


def _ambiguous_detail(kinds: tuple[LibraryDocumentKind, ...]) -> str:
    return "The document matches " + " and ".join(kind.value for kind in kinds) + "."


@router.post(
    LIBRARY_ADDITIONS_PATH,
    response_model=LibraryAdditionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": LibraryAdditionResource}},
)
async def add_library_document_route(
    request: Request,
    actor: str,
    activated_at: str,
    kind: CatalogIntakeKind,
    context: ApiContext = api_context_dependency,
) -> JSONResponse:
    """Store opaque bytes under the caller's required declared kind."""
    require_media_type(request, "application/octet-stream")
    for field in (actor, activated_at):
        if field is None:
            continue
        try:
            context.limits.require_field(field)
        except ApiLimitExceeded as error:
            raise ApiProblem("invalid-request") from error
    try:
        catalog_actor = CatalogActor(actor)
        activated = CatalogActivatedAt(activated_at)
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.admit_library_addition(
            document, kind, catalog_actor, activated
        ),
    )
    match result:
        case LibraryAdditionStored(entry):
            status = HTTPStatus.CREATED
        case LibraryAdditionExisting(entry):
            status = HTTPStatus.OK
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        LibraryAdditionResource(intake_id=entry.intake_id.value, kind=entry.kind.value),
        status,
    )


@router.get(LIBRARY_ADDITION_PATH, response_model=LibraryAdditionResource)
async def get_library_addition_route(
    intake_id: str, context: ApiContext = api_context_dependency
) -> JSONResponse:
    try:
        identifier = CatalogIntakeId(intake_id)
    except ValueError as error:
        raise ApiProblem("invalid-request") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.read_library_addition(identifier),
    )
    match result:
        case LibraryAdditionFound(entry):
            return resource_response(
                LibraryAdditionResource(
                    intake_id=entry.intake_id.value, kind=entry.kind.value
                ),
                HTTPStatus.OK,
            )
        case LibraryAdditionMissing():
            raise ApiProblem("invalid-request", "No catalog intake has that identity.")
        case ReadUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.post(
    API_PREFIX + "/workflow-revisions",
    response_model=WorkflowRevisionDetailResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": WorkflowRevisionDetailResource}},
)
async def publish_revision(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, "application/yaml")
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_workflow_revision(document),
    )
    match result:
        case PublicationCreated(read):
            status = HTTPStatus.CREATED
        case PublicationExisting(read):
            status = HTTPStatus.OK
        case PublicationInvalid(detail, WorkflowRefusal()):
            raise ApiProblem("invalid-workflow-document", detail)
        case PublicationInvalid():
            raise ApiProblem("invalid-workflow-document")
        case PublicationCollision():
            raise ApiProblem("revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(workflow_revision_detail_resource(read), status)


@router.get(
    API_PREFIX + "/workflow-revisions",
    response_model=AnyWorkflowRevisionPageResource,
)
async def list_revisions(
    after_revision_hash: RevisionHashQuery | None = None,
    limit: PageLimitQuery = DEFAULT_PAGE_LIMIT,
    view: str = RevisionListingView.SUMMARY.value,
    context: ApiContext = api_context_dependency,
) -> AnyWorkflowRevisionPageResource:
    """List published revisions in the representation the caller asked for.

    The summary is the default because it is the cheaper read and because it is
    the shape this path has always answered with; the described representation
    costs a parse per revision and is therefore requested, never assumed.
    """

    after = None
    if after_revision_hash is not None:
        after = _revision_hash_or_refuse(after_revision_hash, parse_revision_hash)
    if parse_revision_view(view) is RevisionListingView.SUMMARY:
        return await _summary_page(context, after, limit)
    return await _described_page(context, after, limit)


async def _summary_page(
    context: ApiContext, after: WorkflowRevisionHash | None, limit: int
) -> WorkflowRevisionPageResource:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_workflow_revisions(after, limit),
    )
    match result:
        case WorkflowRevisionsListed(revision_hashes, next_after):
            return WorkflowRevisionPageResource(
                items=tuple(
                    WorkflowRevisionSummaryResource(workflow_revision_hash=value.value)
                    for value in revision_hashes
                ),
                next_after_revision_hash=(
                    None if next_after is None else next_after.value
                ),
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


async def _described_page(
    context: ApiContext, after: WorkflowRevisionHash | None, limit: int
) -> VersionedWorkflowRevisionPageResource:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_described_workflow_revisions(after, limit),
    )
    match result:
        case WorkflowRevisionsDescribed():
            return workflow_revision_page_resource(result)
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def _asked_position(position: str) -> object:
    try:
        return int(position)
    except ValueError:
        return position


@router.post(
    CATALOG_LINEAGES_PATH,
    response_model=CatalogAdmissionResource,
    status_code=201,
)
async def found_catalog_lineage_route(
    request: FoundCatalogLineageRequestResource,
    context: ApiContext = api_context_dependency,
) -> CatalogAdmissionResource:
    """Admit one revision of any kind under the name its authoring format owns."""

    try:
        display_name = (
            None
            if request.display_name is None
            else CatalogLineageDisplayName(request.display_name)
        )
        actor = CatalogActor(request.actor)
        activated_at = CatalogActivatedAt(request.activated_at)
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error

    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.found_catalog_lineage(
            request.kind,
            PublishedRevisionHash(request.catalog_revision_hash),
            display_name,
            actor,
            activated_at,
        ),
    )
    return catalog_admission_resource(result)


@router.get(
    CATALOG_REVISION_BY_NAME_PATH,
    response_model=CatalogNameResolutionResource,
)
async def get_revision_by_name(
    kind: RevisionKind,
    name: str,
    position: str = "head",
    context: ApiContext = api_context_dependency,
) -> CatalogNameResolutionResource:
    """Answer which revision a catalog name holds under one kind.

    The kind is part of the address because a name is only unique within one:
    a workflow and an agent may both be called `review-bounded-diff`, and each
    names its own lineage.
    """

    try:
        query = catalog_lineage_query(name)
    except ValueError as error:
        raise ApiProblem("catalog-name-not-found") from error
    # The application owns which positions are legal; the route only turns the
    # query string into the value it judges, so "7" and "head" travel as the
    # types that rule reads and "later" arrives as the string it refuses.
    asked: object = position if position == "head" else _asked_position(position)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.resolve_catalog_name(kind, query, asked),
    )
    match result:
        case CatalogNameResolved(lineage_id, revision, revision_number, display_name):
            return CatalogNameResolutionResource(
                display_name=display_name.value,
                lineage_id=lineage_id.value,
                catalog_revision_hash=revision.revision_hash.value,
                revision_number=revision_number,
            )
        case CatalogNameLineageRetired():
            raise ApiProblem("catalog-lineage-retired")
        case CatalogNameMissing():
            raise ApiProblem("catalog-name-not-found")
        case CatalogNameInvalidPosition():
            raise ApiProblem("invalid-catalog-position")
        case CatalogReferenceNonMember():
            raise ApiProblem("catalog-revision-not-a-member")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(
    API_PREFIX + "/workflow-revisions/{workflow_revision_hash}",
    response_model=WorkflowRevisionDetailResource,
)
async def get_revision(
    workflow_revision_hash: RevisionHashPath,
    context: ApiContext = api_context_dependency,
) -> WorkflowRevisionDetailResource:
    parsed = _revision_hash_or_refuse(workflow_revision_hash, parse_revision_hash)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_workflow_revision(parsed),
    )
    match result:
        case WorkflowRevisionRead():
            return workflow_revision_detail_resource(result)
        case WorkflowRevisionNotFound():
            raise ApiProblem("workflow-revision-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
