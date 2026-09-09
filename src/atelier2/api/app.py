from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Lifespan

from atelier2.api.context import (
    ApiContext,
    ApiPorts,
    ApiUseCases,
    install_api_context,
)
from atelier2.api.limits import (
    ApiLimits,
    RequestBodyLimitMiddleware,
    durable_projection_limit,
)
from atelier2.api.openapi import API_PREFIX, install_custom_openapi
from atelier2.api.problems import install_problem_handlers
from atelier2.api.routes import (
    agents,
    artifacts,
    catalog_lineage,
    events,
    health,
    models,
    project_source_connection,
    projects,
    queue,
    revisions,
    runs,
    seat,
)
from atelier2.api.seat import SeatReader, no_seat_declared
from atelier2.api.stream import BoundedQueryRunner, EventPollBackoff
from atelier2.application.admit_catalog_member import (
    admit_catalog_member,
    found_catalog_lineage,
)
from atelier2.application.admit_library_addition import (
    admit_library_addition,
    read_library_addition,
)
from atelier2.application.admit_queue_item import (
    confirm_queue_proposal,
    list_queue_items,
)
from atelier2.application.answer_wait import answer_wait_result
from atelier2.application.cancel_agent_attempt import cancel_agent_attempt
from atelier2.application.cancel_run import cancel_run_result
from atelier2.application.classify_definition_document import (
    classify_definition_document,
)
from atelier2.application.fork_run import fork_run
from atelier2.application.import_project_source_issues import (
    import_project_source_issues,
)
from atelier2.application.model_configuration import (
    get_model_registry,
    get_project_model_defaults,
    get_project_model_resolution,
    publish_model_registry,
    publish_project_model_defaults,
    validate_model_registry_entry,
)
from atelier2.application.plan_queue_item import (
    plan_queue_item,
    put_queue_project_policy,
)
from atelier2.application.prepare_run_events import prepare_run_events
from atelier2.application.project_connections import (
    connect_managed_project_source,
    disconnect_project_source,
    get_served_project_source_connection,
    list_served_project_sources,
    new_project_source_id,
    rotate_project_source_token,
)
from atelier2.application.publish_adapter_operation_revision import (
    publish_adapter_operation_revision,
)
from atelier2.application.publish_agent_configurations import (
    publish_agent_configuration_revision,
    publish_auth_profile_revision,
)
from atelier2.application.publish_agent_definition_revision import (
    publish_agent_definition_revision,
)
from atelier2.application.publish_artifact import publish_artifact
from atelier2.application.publish_budget_revision import publish_budget_revision
from atelier2.application.publish_schema_revision import (
    get_schema_revision,
    publish_schema_revision,
)
from atelier2.application.publish_tool_grant_revision import publish_tool_grant_revision
from atelier2.application.publish_workflow_revision import (
    WorkflowPublicationLimits,
    publish_workflow_revision,
)
from atelier2.application.read_agent_configurations import (
    list_agent_configuration_revisions,
    list_auth_profile_revisions,
)
from atelier2.application.read_agent_definition_revisions import (
    get_agent_definition_revision,
    list_agent_definition_revisions,
)
from atelier2.application.read_artifact import read_artifact
from atelier2.application.read_attention_events import read_attention_events
from atelier2.application.read_projects import get_project, list_projects
from atelier2.application.read_redeploy_status import read_redeploy_status
from atelier2.application.read_run_events import read_run_events
from atelier2.application.read_runs import (
    get_node_detail,
    get_run,
    list_runs,
)
from atelier2.application.read_workflow_revisions import (
    get_workflow_revision,
    list_described_workflow_revisions,
    list_workflow_revisions,
)
from atelier2.application.reconcile_run import reconcile_run
from atelier2.application.resolve_catalog_name import resolve_catalog_name
from atelier2.application.retire_catalog_lineage import retire_catalog_lineage
from atelier2.application.start_published_run import start_published_run
from atelier2.contracts.host_configuration import ProjectId, ProjectSourceId
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.contracts.workflow_projections import (
    EnrichedPageBudget,
)
from atelier2.ports.durable_run_forks import DurableRunForker


def _bind_workflow_revision_read_use_cases(
    ports: ApiPorts, enriched_page_budget: EnrichedPageBudget
):
    return (
        lambda revision_hash: get_workflow_revision(
            revision_hash,
            ports.workflow_revision_queries,
            ports.published_revision_registry,
        ),
        lambda after, limit: list_workflow_revisions(
            after, limit, ports.workflow_revision_queries
        ),
        lambda after, limit: list_described_workflow_revisions(
            after,
            limit,
            enriched_page_budget,
            ports.workflow_revision_queries,
            ports.published_revision_resolver_sessions,
        ),
    )


def _bind_run_read_use_cases(ports: ApiPorts):
    return (
        lambda run_id: get_run(run_id, ports.run_queries),
        lambda run_id, node_id: get_node_detail(run_id, node_id, ports.run_queries),
        lambda after, limit, state=None: list_runs(
            after, limit, ports.run_queries, state
        ),
        lambda run_id, after_sequence: prepare_run_events(
            run_id, after_sequence, ports.run_event_queries
        ),
        lambda run_id, after_sequence, page_size: read_run_events(
            run_id, after_sequence, page_size, ports.run_event_queries
        ),
        lambda after_run_id, after_sequence, page_size, excluded_identities=(): (
            read_attention_events(
                after_run_id,
                after_sequence,
                page_size,
                ports.run_event_queries,
                excluded_identities,
            )
        ),
    )


def _bind_content_publication_use_cases(
    ports: ApiPorts, projection_limit: WorkflowPublicationLimits
):
    return (
        lambda document: publish_workflow_revision(
            document,
            ports.workflow_revision_publisher,
            ports.workflow_document_parser,
            projection_limit,
            ports.published_revision_registry,
        ),
        lambda content: publish_artifact(content, ports.artifact_publisher),
        lambda artifact_hash: read_artifact(artifact_hash, ports.artifact_reader),
    )


def _bind_definition_document_use_cases(ports: ApiPorts):
    return (
        lambda document: publish_schema_revision(
            document, ports.published_revision_registry
        ),
        lambda revision_hash: get_schema_revision(
            revision_hash, ports.published_revision_registry
        ),
        lambda document: publish_budget_revision(
            document, ports.published_revision_registry
        ),
        lambda document: publish_tool_grant_revision(
            document, ports.published_revision_registry
        ),
        lambda document: publish_adapter_operation_revision(
            document, ports.published_revision_registry
        ),
        lambda document: publish_agent_definition_revision(
            document,
            ports.agent_definition_parser,
            ports.agent_definition_renderer,
            ports.published_revision_registry,
        ),
        lambda document, file_name: classify_definition_document(
            document,
            file_name,
            ports.workflow_document_parser,
            ports.agent_definition_parser,
        ),
        lambda document, kind, actor, activated_at: admit_library_addition(
            document, kind, actor, activated_at, ports.catalog_intakes
        ),
        lambda intake_id: read_library_addition(intake_id, ports.catalog_intakes),
    )


def _bind_agent_catalog_use_cases(ports: ApiPorts):
    return (
        lambda after, limit: list_agent_definition_revisions(
            after,
            limit,
            ports.published_revision_listing,
            ports.agent_definition_parser,
        ),
        lambda revision_hash: get_agent_definition_revision(
            revision_hash,
            ports.published_revision_registry,
            ports.agent_definition_parser,
        ),
        lambda profile_id, revision_number, provider_id, auth_mode: (
            publish_auth_profile_revision(
                profile_id,
                revision_number,
                provider_id,
                auth_mode,
                ports.agent_configuration_catalog,
            )
        ),
        lambda model, auth_profile_hash, executor_revision, capability: (
            publish_agent_configuration_revision(
                model,
                auth_profile_hash,
                executor_revision,
                capability,
                ports.agent_configuration_catalog,
            )
        ),
        lambda after, limit: list_agent_configuration_revisions(
            after, limit, ports.agent_configuration_catalog
        ),
        lambda after, limit: list_auth_profile_revisions(
            after, limit, ports.agent_configuration_catalog
        ),
    )


def _bind_project_use_cases(served_project_id: ProjectId | None, ports: ApiPorts):
    return (
        lambda: list_projects(served_project_id, ports.host_configuration_channel),
        lambda project_id: get_project(
            project_id, served_project_id, ports.host_configuration_channel
        ),
    )


def _bind_model_configuration_use_cases(
    served_project_id: ProjectId | None, ports: ApiPorts
):
    return (
        lambda provider_id: get_model_registry(
            provider_id, ports.host_configuration_channel
        ),
        lambda provider_id, revision_number, entries: publish_model_registry(
            provider_id,
            revision_number,
            entries,
            ports.host_configuration_channel,
            ports.agent_configuration_catalog,
            ports.model_registry_discoverer,
        ),
        lambda provider_id, configuration_hash: validate_model_registry_entry(
            provider_id,
            configuration_hash,
            ports.host_configuration_channel,
            ports.agent_configuration_catalog,
            ports.model_registry_validator,
        ),
        lambda project_id: get_project_model_defaults(
            project_id, served_project_id, ports.host_configuration_channel
        ),
        lambda project_id, revision_number, defaults: publish_project_model_defaults(
            project_id,
            served_project_id,
            revision_number,
            defaults,
            ports.host_configuration_channel,
        ),
        lambda project_id, workflow_revision_hash, overrides: (
            get_project_model_resolution(
                project_id,
                served_project_id,
                workflow_revision_hash,
                overrides,
                ports.host_configuration_channel,
                ports.workflow_revision_queries,
                ports.agent_configuration_catalog,
            )
        ),
    )


def _bind_run_control_use_cases(served_project_id: ProjectId | None, ports: ApiPorts):
    return (
        lambda run_id, revision_hash, bindings, orders=(): start_published_run(
            run_id,
            revision_hash,
            bindings,
            ports.published_run_starter,
            orders,
            served_project_id,
            ports.tracker_item_source,
        ),
        lambda origin_run_id, idempotency_key, restart_from_node_id: fork_run(
            origin_run_id,
            idempotency_key,
            restart_from_node_id,
            cast(DurableRunForker, ports.published_run_starter),
        ),
        lambda run_id, revision_hash, node_id, execution_id, actor, answer_bytes: (
            answer_wait_result(
                run_id,
                revision_hash,
                node_id,
                execution_id,
                actor,
                answer_bytes,
                ports.wait_answerer,
            )
        ),
        lambda request: reconcile_run(
            request, ports.run_queries, ports.reconcile_commander
        ),
        lambda request: cancel_agent_attempt(request, ports.agent_attempt_canceller),
        lambda run_id, idempotency_key, expected_node_execution_id: cancel_run_result(
            run_id,
            idempotency_key,
            expected_node_execution_id,
            ports.agent_attempt_canceller,
        ),
    )


def _bind_catalog_lineage_use_cases(ports: ApiPorts):
    return (
        lambda kind, query, position: resolve_catalog_name(
            kind, query, position, ports.catalog_resolver
        ),
        lambda kind, revision_hash, name, actor, at: found_catalog_lineage(
            kind,
            revision_hash,
            name,
            actor,
            at,
            ports.catalog_resolver,
            ports.catalog_admissions,
            ports.workflow_document_parser,
            ports.agent_definition_parser,
            ports.workflow_revision_queries,
        ),
        lambda kind, lineage_id, revision_hash, actor, at: admit_catalog_member(
            kind,
            lineage_id,
            revision_hash,
            actor,
            at,
            ports.catalog_resolver,
            ports.catalog_admissions,
            ports.workflow_document_parser,
            ports.agent_definition_parser,
            ports.workflow_revision_queries,
        ),
        lambda lineage_id, actor, at: retire_catalog_lineage(
            lineage_id, actor, at, ports.catalog_admissions
        ),
    )


def _bind_project_source_connection_use_cases(
    served_project_id: ProjectId | None,
    ports: ApiPorts,
    source_id_generator: Callable[[], ProjectSourceId],
    connection_clock: Callable[[], RecordedAt],
):
    return (
        lambda project_id: get_served_project_source_connection(
            project_id,
            served_project_id,
            ports.host_configuration_channel,
            ports.project_source_connection_channel,
            ports.project_source_connector,
        ),
        lambda project_id: list_served_project_sources(
            project_id,
            served_project_id,
            ports.host_configuration_channel,
            ports.project_source_connection_channel,
            ports.project_source_connector,
        ),
        lambda project_id, address, token: connect_managed_project_source(
            project_id,
            served_project_id,
            address,
            token,
            ports.host_configuration_channel,
            ports.project_source_connection_channel,
            ports.project_source_connector,
            ports.project_source_credential_store,
            source_id_generator,
            connection_clock,
        ),
        lambda project_id, source_id: disconnect_project_source(
            project_id,
            served_project_id,
            source_id,
            ports.host_configuration_channel,
            ports.project_source_connection_channel,
        ),
        lambda project_id, source_id, token: rotate_project_source_token(
            project_id,
            served_project_id,
            source_id,
            token,
            ports.host_configuration_channel,
            ports.project_source_connection_channel,
            ports.project_source_connector,
            ports.project_source_credential_store,
        ),
    )


def _bind_queue_use_cases(served_project_id: ProjectId | None, ports: ApiPorts):
    return (
        lambda command: confirm_queue_proposal(command, ports.queue_projection),
        lambda command: plan_queue_item(command, ports.queue_projection),
        lambda policy, expected_revision: put_queue_project_policy(
            policy, expected_revision, ports.queue_projection
        ),
        lambda after, limit: list_queue_items(after, limit, ports.queue_projection),
        lambda: import_project_source_issues(
            served_project_id, ports.tracker_item_source, ports.queue_projection
        ),
        lambda: read_redeploy_status(ports.redeploy_status_reader),
    )


def bound_use_cases(
    ports: ApiPorts,
    projection_limit: WorkflowPublicationLimits,
    enriched_page_budget: EnrichedPageBudget,
    served_project_id: ProjectId | None,
    source_id_generator: Callable[[], ProjectSourceId],
    connection_clock: Callable[[], RecordedAt],
) -> ApiUseCases:
    """Spend the ports here, so that nothing below this line can reach one.

    Each `_bind_*_use_cases` helper below hands back one contiguous slice of
    `ApiUseCases`, in the exact field order that dataclass declares -- the only
    contract this splice relies on -- so the positional unpacking below stays
    exactly as type-checked per call as a flat keyword literal would be,
    without duplicating each field's type here a second time.
    """
    return ApiUseCases(
        *_bind_workflow_revision_read_use_cases(ports, enriched_page_budget),
        *_bind_run_read_use_cases(ports),
        *_bind_content_publication_use_cases(ports, projection_limit),
        *_bind_definition_document_use_cases(ports),
        *_bind_agent_catalog_use_cases(ports),
        *_bind_project_use_cases(served_project_id, ports),
        *_bind_model_configuration_use_cases(served_project_id, ports),
        *_bind_run_control_use_cases(served_project_id, ports),
        *_bind_catalog_lineage_use_cases(ports),
        *_bind_project_source_connection_use_cases(
            served_project_id, ports, source_id_generator, connection_clock
        ),
        *_bind_queue_use_cases(served_project_id, ports),
    )


# The order of these router includes is the order of the published document's
# `paths` keys, which the frozen artefact pins byte for byte.
def _install_routers(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(seat.router)
    app.include_router(agents.router)
    app.include_router(artifacts.router)
    app.include_router(revisions.router)
    app.include_router(catalog_lineage.router)
    app.include_router(projects.router)
    app.include_router(models.router)
    app.include_router(project_source_connection.router)
    app.include_router(runs.router)
    app.include_router(events.router)
    app.include_router(queue.router)


def create_app(
    *,
    source_commit: str,
    source_tree: str,
    ports: ApiPorts,
    limits: ApiLimits,
    event_poll_backoff: EventPollBackoff,
    frontend_dist: Path | None = None,
    served_project_id: ProjectId | None = None,
    seat_reader: SeatReader = no_seat_declared,
    lifespan: Lifespan[FastAPI] | None = None,
    source_id_generator: Callable[[], ProjectSourceId] = new_project_source_id,
    connection_clock: Callable[[], RecordedAt] = recorded_instant,
    boot_clock: Callable[[], RecordedAt] = recorded_instant,
) -> FastAPI:
    if not source_commit:
        raise ValueError("source_commit must be injected at application construction")
    if not source_tree:
        raise ValueError("source_tree must be injected at application construction")
    serve_started_at = boot_clock()
    openapi_document_path = API_PREFIX + "/openapi.json"
    app = FastAPI(
        title="Atelier 2 durable workflow API",
        version="1",
        openapi_url=openapi_document_path,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    install_problem_handlers(
        app,
        versioned_run_start_path=API_PREFIX + "/runs",
        openapi_document_path=openapi_document_path,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        maximum_body_bytes=limits.maximum_request_body_bytes,
        api_prefix=API_PREFIX,
    )
    admission_timeout_seconds = limits.maximum_query_admission_wait_milliseconds / 1_000
    workflow_projection_limit = durable_projection_limit(limits)
    install_api_context(
        app,
        ApiContext(
            source_commit=source_commit,
            source_tree=source_tree,
            serve_started_at=serve_started_at,
            seat=seat_reader,
            use_cases=bound_use_cases(
                ports,
                workflow_projection_limit,
                EnrichedPageBudget(
                    maximum_nodes=limits.maximum_enriched_page_nodes,
                    maximum_document_bytes=limits.maximum_enriched_page_document_bytes,
                ),
                served_project_id,
                source_id_generator,
                connection_clock,
            ),
            ports=ports,
            limits=limits,
            control_runner=BoundedQueryRunner(
                limits.maximum_control_queries,
                admission_timeout_seconds=admission_timeout_seconds,
            ),
            event_runner=BoundedQueryRunner(
                limits.maximum_event_poll_queries,
                admission_timeout_seconds=admission_timeout_seconds,
            ),
            workflow_projection_limit=workflow_projection_limit,
            event_poll_backoff=event_poll_backoff,
            request_queue_sweep=ports.request_queue_sweep,
        ),
    )

    if frontend_dist is not None:
        _mount_frontend(app, frontend_dist)

    # Not an APIRoute: those paths are the browser's served page list.
    app.add_route(
        "/",
        _redirect_host_root,
        methods=["GET"],
        include_in_schema=False,
    )

    _install_routers(app)

    install_custom_openapi(app, limits)
    return app


# Every path the browser can be given cold — reloaded, pasted, bookmarked — must
# hand back the application instead of a route refusal, and which paths those are
# is the client router's decision, not this server's. The browser declares them in
# `frontend/src/lib/servedPaths.json`; `tests/host/test_local_host.py` reads that
# declaration and fails when this tuple does not serve exactly it, because the two
# runtimes cannot import one list.
COCKPIT_INDEX_PATHS: tuple[str, ...] = (
    "/atelier",
    "/atelier/",
    "/atelier/chat",
    "/atelier/settings",
    "/atelier/runs/{public_ref}",
    "/atelier/catalog",
    "/atelier/catalog/{workflow_name:path}",
    "/atelier/history",
)
COCKPIT_HOME_PATH = next(path for path in COCKPIT_INDEX_PATHS if path.endswith("/"))


async def _redirect_host_root(_request: Request) -> RedirectResponse:
    return RedirectResponse(COCKPIT_HOME_PATH, status_code=307)


def _mount_frontend(app: FastAPI, frontend_dist: Path) -> None:
    index_file, assets_directory = _frontend_distribution(frontend_dist)
    app.mount(
        "/atelier/assets",
        StaticFiles(directory=assets_directory, check_dir=True),
        name="atelier-assets",
    )

    async def frontend_index() -> FileResponse:
        return FileResponse(index_file, media_type="text/html")

    for path in COCKPIT_INDEX_PATHS:
        app.add_api_route(
            path,
            frontend_index,
            methods=["GET"],
            include_in_schema=False,
        )


def _frontend_distribution(frontend_dist: Path) -> tuple[Path, Path]:
    distribution = frontend_dist.resolve()
    index_file = distribution / "index.html"
    assets_directory = distribution / "assets"
    if not index_file.is_file() or not assets_directory.is_dir():
        raise ValueError(
            "frontend distribution must contain a readable index.html and assets directory"
        )
    try:
        index_file.open("rb").close()
        next(assets_directory.iterdir(), None)
    except OSError as error:
        raise ValueError("frontend distribution must be readable") from error
    return index_file, assets_directory
