from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Never

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.adapters.yaml_workflows import (
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.limits import ApiLimits
from atelier2.api.openapi import API_PREFIX
from atelier2.api.stream import EventPollBackoff
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.application.read_run_events import ReadRunEventsResult, read_run_events
from atelier2.contracts.agents import (
    AgentBindingSet,
    AgentConfigurationRevision,
    AuthProfileRevision,
)
from atelier2.contracts.host_configuration import ProjectId, ProviderModelCheck
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import (
    RunPage,
    RunProjection,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.host_configuration import (
    ProviderModelDiscoverer,
    ProviderModelDiscovery,
    ProviderModelDiscoveryResult,
    ProviderModelValidationResult,
    ProviderModelValidator,
)
from atelier2.ports.issue_observation import TrackerItemSource
from atelier2.ports.run_events import (
    RunEventQueries,
)
from atelier2.ports.run_queries import (
    RunFound,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
)


class UnusedPort:
    """A port the route under test must not reach."""

    def __getattr__(self, name: str) -> Never:
        raise AssertionError(f"the route under test reached the {name} port")


def unused_attention_event_page(
    after_run_id: object,
    after_sequence: object,
    limit: object,
    excluded_identities: object = (),
) -> Never:
    del after_run_id, after_sequence, limit, excluded_identities
    raise AssertionError("attention feed was not under test")


STREAM_DOCUMENT = b"""format_version: 3
name: One agent, streamed
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: Produce the payload this stream carries.
    outputs:
      - name: payload
        schema: {ref: workspace_candidate, revision: schema-candidate}
"""


def stream_run_projection(run_id: str) -> RunProjection:
    """The run an SSE scenario streams, so every frame can say where it stands.

    The event endpoint reads the run once and folds the streamed events onto it.
    A stream scenario therefore needs a run, not only an event store, and this is
    the one place that decides which run that is.
    """

    revision = WorkflowRevision(STREAM_DOCUMENT)
    return RunProjection(
        RunV3(
            RunId(run_id),
            revision.revision_hash,
            AgentBindingSet(()).binding_set_hash,
            (),
            RunState.STARTED,
            "agent",
            0,
            0,
            RunConfigurationRevisionHash("c" * 64),
        ),
        parse_executable_workflow_document(STREAM_DOCUMENT),
        None,
    )


class OneRunQueries:
    """A run store that answers with one projection, whichever run is asked for."""

    def __init__(self, projection: RunProjection) -> None:
        self._projection = projection

    def get_run(self, run_id: object, projection_limit: object = None) -> RunFound:
        del run_id, projection_limit
        return RunFound(self._projection)


class ExactConfiguredProviderModels:
    """The deterministic provider boundary used by API integration scenarios."""

    def discover_models(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelDiscoveryResult:
        del auth_profile
        return ProviderModelDiscovery(frozenset({configuration.model}))

    def validate_model(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelValidationResult:
        del configuration, auth_profile
        return ProviderModelCheck.CHECKED


def api_ports(**overrides: object) -> ApiPorts:
    """The full port set with only the ports a test names actually wired."""
    unused = UnusedPort()
    ports: dict[str, Any] = {
        "workflow_revision_publisher": unused,
        "published_run_starter": unused,
        "wait_answerer": unused,
        "reconcile_commander": unused,
        "workflow_revision_queries": unused,
        "run_queries": unused,
        "run_event_queries": unused,
        "workflow_document_parser": parse_workflow_document,
        "agent_definition_parser": parse_agent_definition,
        "agent_definition_renderer": render_agent_definition,
        "agent_configuration_catalog": unused,
        "agent_attempt_canceller": unused,
        "catalog_resolver": unused,
        "catalog_admissions": unused,
        "library_additions": unused,
        "catalog_intakes": unused,
        "published_revision_registry": unused,
        "published_revision_resolver_sessions": unused,
        "published_revision_listing": unused,
        "artifact_publisher": unused,
        "artifact_reader": unused,
        "host_configuration_channel": unused,
        "project_source_connection_channel": unused,
        "project_source_connector": unused,
        "project_source_credential_store": unused,
        "queue_projection": unused,
        "model_registry_discoverer": ExactConfiguredProviderModels(),
        "model_registry_validator": ExactConfiguredProviderModels(),
    }
    ports.update(overrides)
    return ApiPorts(**ports)


def durable_ports(
    engine: Engine,
    settings: DbosRuntimeSettings,
    agent_executor_registry: AgentExecutorRegistry,
    queries: DbosQueries | None = None,
    **overrides: object,
) -> ApiPorts:
    """The real DBOS-backed port set every durable test composes identically.

    This is the one production shape `atelier2.host.serving` wires, built for a
    test engine instead. `queries` is the one port call sites genuinely
    disagree on -- some share a plain reader across all three query ports,
    one bounds it to a publication limit -- so it is the one named parameter;
    anything else a caller passes overrides the matching field verbatim.
    """
    resolved_queries = queries if queries is not None else durable_queries(engine)
    catalog = DbosCatalogStore(engine)
    unused = UnusedPort()
    ports: dict[str, Any] = {
        "workflow_revision_publisher": DbosWorkflowRevisionPublisher(engine),
        "published_run_starter": DbosDurableRunStarter(
            engine,
            settings,
            agent_executor_registry,
        ),
        "wait_answerer": DbosWaitAnswerer(engine, settings.application_version),
        "reconcile_commander": DbosEffectReconcileCommander(engine, settings),
        "workflow_revision_queries": resolved_queries,
        "run_queries": resolved_queries,
        "run_event_queries": resolved_queries,
        "workflow_document_parser": parse_workflow_document,
        "agent_definition_parser": parse_agent_definition,
        "agent_definition_renderer": render_agent_definition,
        "agent_configuration_catalog": DbosAgentConfigurationCatalog(
            engine, agent_executor_registry
        ),
        "agent_attempt_canceller": DbosAgentAttemptStore(
            engine, settings.application_version
        ),
        "catalog_resolver": catalog,
        "catalog_admissions": catalog,
        "library_additions": catalog,
        "catalog_intakes": catalog,
        "published_revision_registry": catalog,
        "published_revision_resolver_sessions": catalog,
        "published_revision_listing": catalog,
        "artifact_publisher": DbosArtifactStore(engine),
        "artifact_reader": DbosArtifactStore(engine),
        "host_configuration_channel": DbosHostConfigurationChannel(engine),
        "project_source_connection_channel": DbosHostConfigurationChannel(engine),
        "project_source_connector": unused,
        "project_source_credential_store": unused,
        "queue_projection": DbosQueueProjectionStore(engine),
        "model_registry_discoverer": ExactConfiguredProviderModels(),
        "model_registry_validator": ExactConfiguredProviderModels(),
    }
    ports.update(overrides)
    return ApiPorts(**ports)


def api_limits(**changes: int) -> ApiLimits:
    event_page_size = changes.pop("event_page_size", 50)
    configured = ApiLimits(
        maximum_request_body_bytes=65_536,
        maximum_field_characters=1_024,
        maximum_base64_characters=65_536,
        maximum_decoded_payload_bytes=49_152,
        maximum_workflow_nodes=100,
        maximum_enriched_page_nodes=100,
        maximum_enriched_page_document_bytes=65_536,
        event_page_size=PageLimit(event_page_size),
        maximum_control_queries=8,
        maximum_event_poll_queries=2,
        maximum_query_admission_wait_milliseconds=1_000,
    )
    return replace(configured, **changes)


def event_poll_backoff() -> EventPollBackoff:
    return EventPollBackoff(0.01, 0.25, 2)


def event_stream_client(queries: RunEventQueries) -> TestClient:
    """The composed HTTP boundary in front of one event store and nothing else.

    What the event endpoint answers is a property of the composed server, so a
    test that claims an HTTP status, a problem body, or stream bytes asks for
    it here instead of calling the generator by hand.
    """

    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                run_event_queries=queries,
                run_queries=OneRunQueries(stream_run_projection("sse/run")),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def described_api_client() -> TestClient:
    """The composed HTTP boundary in front of no store at all.

    What the API says about itself -- its document, and the refusal that names
    where the document is -- is answered before any port is reached, so a test
    that reads the description wires none.
    """

    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def named_document_path(refusal_detail: str) -> str:
    """The one path a refusal names, read out of the sentence that names it."""

    named = [
        word.rstrip(".") for word in refusal_detail.split() if word.startswith("/")
    ]
    return named[-1]


def discovered_openapi_document(client: TestClient, guessed_path: str) -> Any:
    """The description found the way a consumer must find it: knock, then read.

    A first contact holds a base URL and nothing else. The path it guesses is
    refused, the refusal names where the description lives, and that is the only
    route by which a test here learns the document's own address.
    """

    refusal = client.get(guessed_path)
    return client.get(named_document_path(refusal.json()["detail"])).json()


def published_workflow_grammar_reference(openapi_document: Any) -> Any:
    """What the publication body names as the document it takes.

    Followed the way a consumer has to follow it -- publication path, its one
    declared body, the reference that body names -- so nothing a test knows
    about the shape came from the repository the API is served from.
    """

    body = openapi_document["paths"][API_PREFIX + "/workflow-revisions"]["post"][
        "requestBody"
    ]
    (declared,) = body["content"].values()
    return declared["schema"]


def openapi_component(openapi_document: Any, reference: Any) -> Any:
    """One component of a served document, resolved through the reference to it."""

    name = reference["$ref"].rsplit("/", 1)[1]
    return openapi_document["components"]["schemas"][name]


def published_workflow_grammar(openapi_document: Any) -> Draft202012Validator:
    """The published shape, assembled into the check a consumer would run."""

    return Draft202012Validator(
        {**openapi_document, **published_workflow_grammar_reference(openapi_document)}
    )


def durable_asgi_app(
    runtime: DbosRuntime,
    limits: ApiLimits | None = None,
    poll_backoff: EventPollBackoff | None = None,
    served_project_id: ProjectId | None = None,
    tracker_item_source: TrackerItemSource | None = None,
    model_registry_discoverer: ProviderModelDiscoverer | None = None,
    model_registry_validator: ProviderModelValidator | None = None,
) -> FastAPI:
    """The ASGI app in front of one real durable runtime.

    `durable_api_client` wraps this for a single caller. A concurrent harness
    needs the app itself so one event loop can drive many requests.
    """

    provider_models = ExactConfiguredProviderModels()
    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=durable_ports(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
            tracker_item_source=tracker_item_source,
            model_registry_discoverer=(
                provider_models
                if model_registry_discoverer is None
                else model_registry_discoverer
            ),
            model_registry_validator=(
                provider_models
                if model_registry_validator is None
                else model_registry_validator
            ),
        ),
        limits=api_limits() if limits is None else limits,
        event_poll_backoff=event_poll_backoff()
        if poll_backoff is None
        else poll_backoff,
        served_project_id=served_project_id,
    )


def durable_api_client(
    runtime: DbosRuntime,
    limits: ApiLimits | None = None,
    served_project_id: ProjectId | None = None,
    tracker_item_source: TrackerItemSource | None = None,
    model_registry_discoverer: ProviderModelDiscoverer | None = None,
    model_registry_validator: ProviderModelValidator | None = None,
) -> TestClient:
    """The real HTTP boundary in front of one real durable runtime.

    Whether a request is refused before anything durable exists is a property
    of the composed server, not of a starter called by hand, so a test that
    claims an HTTP answer asks for it here.
    """

    return TestClient(
        durable_asgi_app(
            runtime,
            limits,
            served_project_id=served_project_id,
            tracker_item_source=tracker_item_source,
            model_registry_discoverer=model_registry_discoverer,
            model_registry_validator=model_registry_validator,
        )
    )


def stream_projection_limit() -> WorkflowPublicationLimits:
    """A limit wide enough not to be what a stream test is about."""
    return WorkflowPublicationLimits(
        maximum_document_bytes=65_536,
        maximum_nodes=100,
        maximum_string_characters=1_024,
        maximum_payload_bytes=49_152,
    )


def stream_page_reader(
    queries: RunEventQueries,
    projection_limit: DurableProjectionLimit | None = None,
) -> Callable[[RunId, int, int], ReadRunEventsResult]:
    """The page decision a stream drives, bound to one store.

    Tests hand this to `stream_server_events` rather than the store itself, so the
    real `read_run_events` decision stays on the path they exercise instead of
    being stepped over by a fake shaped like the port. The limit is a real one
    rather than `None`: the use-case takes no fail-open default, which is the
    direction head D is moving the ports in anyway.
    """

    def read_page(
        run_id: RunId, after_sequence: int, page_size: int
    ) -> ReadRunEventsResult:
        return read_run_events(run_id, after_sequence, page_size, queries)

    return read_page


def permissive_projection_limit() -> WorkflowPublicationLimits:
    """A bound wide enough not to be what a test is about."""
    return WorkflowPublicationLimits(
        maximum_document_bytes=1_000_000,
        maximum_nodes=1_000,
        maximum_string_characters=100_000,
        maximum_payload_bytes=1_000_000,
    )


def durable_queries(
    engine: Engine, projection_limit: WorkflowPublicationLimits | None = None
) -> DbosQueries:
    """A durable reader holding a bound, because every reader now holds one.

    A test that is not about the bound takes a permissive one; a test that is
    about it passes its own. Neither can build a reader without one, which is the
    point of the change this helper follows.
    """
    return DbosQueries(engine, projection_limit or permissive_projection_limit())


FROZEN_OPENAPI_DOCUMENT_PATH = (
    Path(__file__).resolve().parents[1] / "api" / "openapi_frozen.json"
)

# The document itself is served at this path (#1501's own route, added by hand
# because FastAPI would otherwise serve it through a bare Starlette route with
# no dependant this guard could ever reach) but is not one of its own listed
# paths (`include_in_schema=False`), so a caller proving the guard covers every
# route names it here rather than finding it in the frozen document.
OPENAPI_DOCUMENT_PATH = API_PREFIX + "/openapi.json"

# The one value per path-parameter name any operation in the frozen document
# needs to reach past FastAPI's own path-shape routing and into a route's own
# body -- never a value chosen to satisfy that route's *own* validation
# (a wrong shape there answers 400 or 404, not 422, so it never reads as this
# guard's own refusal). `kind` and `provider_id` are the two names a route
# enforces before reaching this guard's sibling dependencies (a real
# `Path(pattern=...)`, or an enum on the path itself); every hash-shaped name
# below is a real SHA-256 hex string for the same reason.
QUERY_PATH_PARAMETER_EXAMPLES: dict[str, str] = {
    "artifact_hash": "a" * 64,
    "schema_revision_hash": "a" * 64,
    "agent_definition_revision_hash": "a" * 64,
    "workflow_revision_hash": "a" * 64,
    "intake_id": "a" * 64,
    "lineage_id": "a" * 64,
    "attempt_id": "a" * 64,
    "kind": "workflow",
    "name": "example",
    "public_project_reference": "example",
    "public_source_reference": "example",
    "public_ref": "example",
    "node_id": "example",
    "provider_id": "example",
}

_PATH_PARAMETER = re.compile(r"\{([^}]+)\}")


def resolved_frozen_path(template: str) -> str:
    """One example URL for a frozen-document path template.

    Fails loudly for a path-parameter name this module does not yet carry an
    example for, rather than guessing one at the call site.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in QUERY_PATH_PARAMETER_EXAMPLES, (
            f"no example value registered for path parameter {name!r}; "
            "add one to QUERY_PATH_PARAMETER_EXAMPLES"
        )
        return QUERY_PATH_PARAMETER_EXAMPLES[name]

    return _PATH_PARAMETER.sub(replace, template)


def frozen_document_paths(method: str) -> tuple[str, ...]:
    """Every frozen-document path declaring the named HTTP method (lowercase)."""

    document = json.loads(FROZEN_OPENAPI_DOCUMENT_PATH.read_text())
    return tuple(
        path for path, operations in document["paths"].items() if method in operations
    )


def healthy_runs(page: RunPage) -> tuple[RunProjection, ...]:
    """A page's rows, where the scenario seeded only runs that project cleanly.

    `RunPage.runs` admits a defective row (#1042) because a real page can hold
    one; a scenario that seeded no corrupt run asserts that here once, instead
    of every call site unsafely indexing `.run` into a row that might not have
    one. A defective row reaching this call is the scenario's own defect, not
    a shape to narrow past quietly.
    """
    healthy: list[RunProjection] = []
    for row in page.runs:
        assert isinstance(row, RunProjection), (
            f"expected a healthy listed run, got {row!r}"
        )
        healthy.append(row)
    return tuple(healthy)
