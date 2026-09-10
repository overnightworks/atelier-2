"""The real start door freezes the fixed model precedence into V3 bindings."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos import host_configuration as host_configuration_adapter
from atelier2.adapters.dbos import starter as starter_adapter
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    publish_project_root_revision,
)
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    host_project_model_defaults_revisions,
    run_agent_bindings,
    runs,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import (
    API_PREFIX,
    MODEL_REGISTRY_PATH,
    PROJECT_MODEL_DEFAULTS_PATH,
    PROJECT_MODEL_RESOLUTION_PATH,
)
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    HostModelConfigurationSnapshot,
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ProjectId,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProjectRootRevision,
    ProviderModelCheck,
)
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueItemAdmitted,
    QueueItemProposed,
    QueueItemTrackerObservation,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunState, WorkflowRevision
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows_v3 import RoleDifficulty
from atelier2.host.run_command import AgentRoleBinding, start_request_body
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
)
from atelier2.ports.host_configuration import (
    ModelRegistryRevisionCreated,
    ProjectModelDefaultsRevisionCreated,
)
from atelier2.ports.published_revisions import CatalogLineageFounded
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.api import durable_api_client
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.runs import publish_revision
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

SERVED_PROJECT = ProjectId("atelier")
JSON_MEDIA_TYPE = "application/json"

ONE_ROLE_DOCUMENT = b"""format_version: 3
name: One configured role
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the candidate.
    difficulty: 2
""" + declared_output()

TOOL_BEARING_ROLE_DOCUMENT = b"""format_version: 3
name: One tool-bearing role
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless_with_tools
    instruction: Build the candidate.
    difficulty: 2
""" + declared_output()

ONE_ROLE_IN_TWO_MODES_DOCUMENT = (
    b"""format_version: 3
name: One role asked for two modes
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the candidate.
    difficulty: 2
"""
    + declared_output()
    + b"""  - id: polish
    type: agent
    role: builder
    mode: headless_with_tools
    instruction: Polish the candidate.
    difficulty: 2
    depends_on: [implement]
"""
    + declared_output()
)

EASY_FAMILY_DOCUMENT = (
    b"""format_version: 3
name: Two related roles, both easy
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the candidate.
    difficulty: 1
    family_differs_from: reviewer
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review the candidate.
    difficulty: 1
    depends_on: [implement]
"""
    + declared_output()
)

TWO_ROLE_DOCUMENT = (
    b"""format_version: 3
name: Two configured roles
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the candidate.
    difficulty: 2
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review the candidate.
    difficulty: 3
    depends_on: [implement]
"""
    + declared_output()
)

TWO_ROLE_FAMILY_DOCUMENT = (
    b"""format_version: 3
name: Two related roles
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the candidate.
    difficulty: 2
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review the candidate.
    difficulty: 2
    family_differs_from: builder
    depends_on: [implement]
"""
    + declared_output()
)

THREE_ROLE_PARTIAL_FAMILY_DOCUMENT = (
    b"""format_version: 3
name: Partially satisfiable family chain
nodes:
  - id: alpha
    type: agent
    role: alpha
    mode: headless
    instruction: Do alpha work.
    difficulty: 2
    model: alpha
    family_differs_from: beta
"""
    + declared_output()
    + b"""  - id: beta
    type: agent
    role: beta
    mode: headless
    instruction: Do beta work.
    difficulty: 2
    family_differs_from: gamma
    depends_on: [alpha]
"""
    + declared_output()
    + b"""  - id: gamma
    type: agent
    role: gamma
    mode: headless
    instruction: Do gamma work.
    difficulty: 2
    model: preferred
    depends_on: [beta]
"""
    + declared_output()
)


def started_runtime(
    tmp_path: Path, providers: tuple[str, ...]
) -> Iterator[DbosRuntime]:
    """One runtime whose atelier can run an executor of each named provider."""
    project_root = tmp_path / "project"
    git_project(project_root, declaring_verification(["true"]))
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "model-default-cast-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=SERVED_PROJECT,
            bootstrap_project_root=project_root,
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        tuple(
            RecordingAgentExecutorFactoryV2(
                provider,
                f"{provider}/v1",
                f"{provider}-operation",
                b'"the exact bytes"',
            )
            for provider in providers
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


@pytest.fixture
def runtime(tmp_path: Path, dbos_logging_isolation: None) -> Iterator[DbosRuntime]:
    yield from started_runtime(tmp_path, ("exact",))


@pytest.fixture
def three_provider_runtime(
    tmp_path: Path, dbos_logging_isolation: None
) -> Iterator[DbosRuntime]:
    yield from started_runtime(tmp_path, ("exact", "openai", "anthropic"))


def publish_workflow(
    runtime: DbosRuntime, document: bytes = ONE_ROLE_DOCUMENT
) -> tuple[WorkflowRevision, CatalogLineageId]:
    """Publish one executable document and found the lineage the queue binds."""
    DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    revision = WorkflowRevision(document)
    publish_revision(runtime.engine, revision)
    catalog = DbosCatalogStore(runtime.engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, document)
    catalog.publish_revision(published)
    founded = catalog.found_lineage(
        published,
        CatalogLineageDisplayName("occupied-workflow"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-25T09:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded), founded
    return revision, founded.lineage.lineage_id


def publish_configuration(
    runtime: DbosRuntime,
    model: str,
    capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
    provider: str = "exact",
    profile_id: str | None = None,
) -> AgentConfigurationRevisionHash:
    """One published agent configuration a project default or caller can name."""
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision(
        provider if profile_id is None else profile_id,
        1,
        ProviderId(provider),
        AuthMode.SUBSCRIPTION,
    )
    assert isinstance(
        catalog.publish_auth_profile_revision(auth),
        (AuthProfileRevisionCreated, AuthProfileRevisionExisting),
    )
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        AgentExecutorRevision(f"{provider}/v1"),
        capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    return configuration.revision_hash


def configure_defaults(
    runtime: DbosRuntime,
    models: tuple[tuple[int, str, AgentConfigurationRevisionHash], ...],
    revision_number: int = 1,
    also_registered: tuple[tuple[str, AgentConfigurationRevisionHash], ...] = (),
) -> None:
    """Register these configurations and make the named ones the project's rows.

    `also_registered` is what an operator's registry holds beside the default
    rows -- a second configuration of a default's own model, for instance.
    """
    channel = DbosHostConfigurationChannel(runtime.engine)
    registry = ModelRegistryRevision(
        ProviderId("exact"),
        revision_number,
        tuple(
            ModelRegistryEntry(
                model,
                configuration,
                ModelRegistryEntrySource.OPERATOR,
                ProviderModelCheck.CHECKED,
            )
            for model, configuration in (
                [(model, configuration) for _difficulty, model, configuration in models]
                + list(also_registered)
            )
        ),
    )
    published_registry = channel.publish_model_registry_revision(registry)
    assert isinstance(published_registry, ModelRegistryRevisionCreated)
    published_defaults = channel.publish_project_model_defaults_revision(
        ProjectModelDefaultsRevision(
            SERVED_PROJECT,
            revision_number,
            tuple(
                ProjectModelDefault(
                    cast(RoleDifficulty, difficulty),
                    registry.revision_hash,
                    registry.provider_id,
                    model,
                    configuration,
                )
                for difficulty, model, configuration in models
            ),
        )
    )
    assert isinstance(published_defaults, ProjectModelDefaultsRevisionCreated)


def configure_defaults_across_providers(
    runtime: DbosRuntime,
    steps: tuple[tuple[int, str, str, AgentConfigurationRevisionHash], ...],
) -> None:
    """One registry per provider, and one default row per difficulty over them.

    Difficulty steps that reach for different providers are what a family rule
    needs to have anything to choose between.
    """
    channel = DbosHostConfigurationChannel(runtime.engine)
    registries: dict[str, ModelRegistryRevision] = {}
    for _difficulty, provider, model, configuration in steps:
        held = registries[provider].entries if provider in registries else ()
        registries[provider] = ModelRegistryRevision(
            ProviderId(provider),
            1,
            held
            + (
                ModelRegistryEntry(
                    model,
                    configuration,
                    ModelRegistryEntrySource.OPERATOR,
                    ProviderModelCheck.CHECKED,
                ),
            ),
        )
    for registry in registries.values():
        assert isinstance(
            channel.publish_model_registry_revision(registry),
            ModelRegistryRevisionCreated,
        )
    published = channel.publish_project_model_defaults_revision(
        ProjectModelDefaultsRevision(
            SERVED_PROJECT,
            1,
            tuple(
                ProjectModelDefault(
                    cast(RoleDifficulty, difficulty),
                    registries[provider].revision_hash,
                    ProviderId(provider),
                    model,
                    configuration,
                )
                for difficulty, provider, model, configuration in steps
            ),
        )
    )
    assert isinstance(published, ProjectModelDefaultsRevisionCreated)


def admit_queue_item(runtime: DbosRuntime, lineage_id: CatalogLineageId) -> None:
    """Plan and manually confirm one queue item through the Phase-D contract."""
    store = DbosQueueProjectionStore(runtime.engine)
    reference = WorkItemReference(SERVED_PROJECT, TrackerItemReference("gh:680"))
    observed_at = RecordedAt("2026-08-27T10:00:00Z")
    store.reconcile_open_items(
        SERVED_PROJECT,
        ((reference, QueueItemTrackerObservation("cast the model", observed_at)),),
        observed_at,
    )
    store.put_policy(QueueProjectPolicyRevision(SERVED_PROJECT, 1, 1, None), 0)
    proposed = store.plan(
        PlanQueueItem(
            reference,
            QueueProposal(
                QueuePriorityRank(1),
                lineage_id,
                (),
                QueueAutomationDisposition.HUMAN_REQUIRED,
                1,
            ),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(proposed, QueueItemProposed), proposed
    admitted = store.confirm(
        ConfirmQueueProposal(
            reference,
            proposed.revision,
            QueueAdmissionRationale("the triage rule matched"),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted), admitted


def conductor_start(
    client: TestClient,
    run_id: str,
    revision: WorkflowRevision,
    bindings: tuple[AgentRoleBinding, ...] = (),
) -> Response:
    """POST /runs with the exact body the conductor's `start_run` tool builds."""
    return client.post(
        API_PREFIX + "/runs",
        content=start_request_body(run_id, revision.revision_hash.value, bindings),
        headers={"content-type": JSON_MEDIA_TYPE},
    )


def stored_bindings(engine: Engine, run_id: str) -> list[tuple[str, str]]:
    """The role matrix the run itself carries, in the store's own words."""
    with engine.connect() as connection:
        return [
            (str(record["role"]), str(record["agent_configuration_revision_hash"]))
            for record in connection.execute(
                sa.select(run_agent_bindings)
                .where(run_agent_bindings.c.run_id == run_id)
                .order_by(run_agent_bindings.c.role)
            ).mappings()
        ]


def run_ids(engine: Engine) -> list[str]:
    """Every run this store holds, however it was started."""
    with engine.connect() as connection:
        return [
            str(value)
            for value in connection.execute(sa.select(runs.c.run_id)).scalars()
        ]


def test_the_api_reads_registry_defaults_and_role_resolution(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    configured = publish_configuration(runtime, "opus")
    configure_defaults(runtime, ((2, "opus", configured),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)
    project_reference = encode_public_project_reference(SERVED_PROJECT)

    registry = client.get(MODEL_REGISTRY_PATH.replace("{provider_id}", "exact"))
    defaults = client.get(
        PROJECT_MODEL_DEFAULTS_PATH.replace(
            "{public_project_reference}", project_reference
        )
    )
    resolution = client.post(
        PROJECT_MODEL_RESOLUTION_PATH.replace(
            "{public_project_reference}", project_reference
        ),
        json={"workflow_revision_hash": revision.revision_hash.value, "overrides": []},
    )

    assert registry.status_code == 200
    assert registry.json()["entries"][0] == {
        "model_id": "opus",
        "agent_configuration_revision_hash": configured.value,
        "source": "operator",
        "provider_check": "checked",
    }
    assert defaults.status_code == 200
    assert defaults.json()["defaults"][0]["difficulty"] == 2
    assert resolution.status_code == 200, resolution.text
    assert resolution.json()["resolutions"] == [
        {
            "role": "builder",
            "agent_configuration_revision_hash": configured.value,
            "source": "from-project",
            "model_id": "opus",
            "declared_difficulty": 2,
            "default_difficulty": 2,
            "uncast_reason": None,
            "family_differs_from": None,
        }
    ]


def test_foreign_project_model_operations_stop_before_configuration_access(
    runtime: DbosRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = ProjectId("foreign")
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    publish_project_root_revision(
        runtime.engine, ProjectRootRevision(foreign, 1, foreign_root)
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)
    foreign_reference = encode_public_project_reference(foreign)
    paths = (
        PROJECT_MODEL_DEFAULTS_PATH.replace(
            "{public_project_reference}", foreign_reference
        ),
        PROJECT_MODEL_RESOLUTION_PATH.replace(
            "{public_project_reference}", foreign_reference
        ),
    )

    def unexpected_channel_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("foreign project reached the configuration channel")

    for method in (
        "latest_project_root_revision",
        "latest_project_model_defaults_revision",
        "publish_project_model_defaults_revision",
        "model_configuration_snapshot",
    ):
        monkeypatch.setattr(
            DbosHostConfigurationChannel, method, unexpected_channel_access
        )

    get_response = client.get(paths[0])
    put_response = client.put(paths[0], json={"revision_number": 1, "defaults": []})
    resolution_response = client.post(
        paths[1],
        json={"workflow_revision_hash": "0" * 64, "overrides": []},
    )

    assert [
        get_response.status_code,
        put_response.status_code,
        resolution_response.status_code,
    ] == [404, 404, 404]
    assert all(
        response.json()["type"].endswith(":project-unknown")
        for response in (get_response, put_response, resolution_response)
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    host_project_model_defaults_revisions
                )
            )
            == 0
        )


@pytest.mark.parametrize("provider_id", ["Anthropic", "-anthropic", "anthropic!"])
def test_model_registry_path_refuses_non_provider_ids(
    runtime: DbosRuntime, provider_id: str
) -> None:
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    response = client.get(MODEL_REGISTRY_PATH.replace("{provider_id}", provider_id))

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")


def test_resolution_and_start_each_take_one_transactional_configuration_snapshot(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    configured = publish_configuration(runtime, "opus")
    configure_defaults(runtime, ((2, "opus", configured),))
    original = host_configuration_adapter.model_configuration_snapshot
    snapshots: list[tuple[str, bool]] = []

    def read_snapshot(
        connection: Connection, project_id: ProjectId | None
    ) -> HostModelConfigurationSnapshot:
        snapshots.append(("resolution", connection.in_transaction()))
        return original(connection, project_id)

    def start_snapshot(
        connection: Connection, project_id: ProjectId | None
    ) -> HostModelConfigurationSnapshot:
        snapshots.append(("start", connection.in_transaction()))
        return original(connection, project_id)

    monkeypatch.setattr(
        host_configuration_adapter, "model_configuration_snapshot", read_snapshot
    )
    monkeypatch.setattr(starter_adapter, "model_configuration_snapshot", start_snapshot)
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)
    project_reference = encode_public_project_reference(SERVED_PROJECT)

    resolution = client.post(
        PROJECT_MODEL_RESOLUTION_PATH.replace(
            "{public_project_reference}", project_reference
        ),
        json={"workflow_revision_hash": revision.revision_hash.value, "overrides": []},
    )
    started = conductor_start(client, "conductor/one-snapshot", revision)

    assert resolution.status_code == 200, resolution.text
    assert started.status_code == 201, started.text
    assert snapshots == [("resolution", True), ("start", True)]


def test_a_start_without_bindings_uses_the_projects_difficulty_default(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    configured = publish_configuration(runtime, "opus")
    configure_defaults(runtime, ((2, "opus", configured),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    started = conductor_start(client, "conductor/occupied", revision)

    assert started.status_code == 201, started.text
    assert started.json()["state"] == RunState.STARTED.value
    assert stored_bindings(runtime.engine, "conductor/occupied") == [
        ("builder", configured.value)
    ]


def test_an_explicit_binding_wins_over_the_projects_default(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    configured = publish_configuration(runtime, "opus")
    named = publish_configuration(runtime, "sonnet")
    configure_defaults(runtime, ((2, "opus", configured), (3, "sonnet", named)))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    started = conductor_start(
        client,
        "conductor/explicit",
        revision,
        (AgentRoleBinding("builder", named.value),),
    )

    assert started.status_code == 201, started.text
    assert stored_bindings(runtime.engine, "conductor/explicit") == [
        ("builder", named.value)
    ]


def resolve(client: TestClient, revision: WorkflowRevision) -> Response:
    """POST the start sheet's own question: who would fill each role now?"""
    return client.post(
        PROJECT_MODEL_RESOLUTION_PATH.replace(
            "{public_project_reference}",
            encode_public_project_reference(SERVED_PROJECT),
        ),
        json={"workflow_revision_hash": revision.revision_hash.value, "overrides": []},
    )


def test_a_headless_node_binds_the_headless_configuration_of_its_step_model(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    tool_bearing = publish_configuration(
        runtime, "opus", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    headless = publish_configuration(runtime, "opus")
    configure_defaults(
        runtime, ((2, "opus", tool_bearing),), also_registered=(("opus", headless),)
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    started = conductor_start(client, "conductor/headless-node", revision)

    assert started.status_code == 201, started.text
    assert stored_bindings(runtime.engine, "conductor/headless-node") == [
        ("builder", headless.value)
    ]


def test_a_tool_bearing_node_takes_the_tool_bearing_configuration_of_its_step_model(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime, TOOL_BEARING_ROLE_DOCUMENT)
    headless = publish_configuration(runtime, "opus")
    tool_bearing = publish_configuration(
        runtime, "opus", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    configure_defaults(
        runtime, ((2, "opus", headless),), also_registered=(("opus", tool_bearing),)
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    resolution = resolve(client, revision)

    assert resolution.status_code == 200, resolution.text
    assert resolution.json()["resolutions"][0]["agent_configuration_revision_hash"] == (
        tool_bearing.value
    )


def test_a_step_model_without_the_nodes_mode_leaves_the_role_uncast(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    tool_bearing = publish_configuration(
        runtime, "opus", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    configure_defaults(runtime, ((2, "opus", tool_bearing),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    refused = conductor_start(client, "conductor/mode-unfilled", revision)

    assert refused.json() == {
        "type": "urn:atelier2:problem:v1:uncast-agent-roles",
        "title": "Agent roles need models",
        "status": 422,
        "detail": "Choose a registered model for every workflow role without one.",
        "uncast_roles": [{"role": "builder", "reason": "model-not-in-node-mode"}],
    }
    assert run_ids(runtime.engine) == []


def test_an_exact_binding_override_is_cast_as_it_was_written(
    runtime: DbosRuntime,
) -> None:
    """A start that names one exact configuration is never read for a mode.

    The mismatch such a binding may carry is the start's own refusal to make,
    by name; leaving the role uncast here would say far less.
    """
    revision, _lineage_id = publish_workflow(runtime)
    headless = publish_configuration(runtime, "opus")
    tool_bearing = publish_configuration(
        runtime, "opus", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    configure_defaults(
        runtime, ((2, "opus", headless),), also_registered=(("opus", tool_bearing),)
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    resolution = client.post(
        PROJECT_MODEL_RESOLUTION_PATH.replace(
            "{public_project_reference}",
            encode_public_project_reference(SERVED_PROJECT),
        ),
        json={
            "workflow_revision_hash": revision.revision_hash.value,
            "overrides": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": tool_bearing.value,
                }
            ],
        },
    )

    assert resolution.status_code == 200, resolution.text
    assert resolution.json()["resolutions"][0]["agent_configuration_revision_hash"] == (
        tool_bearing.value
    )
    assert resolution.json()["resolutions"][0]["source"] == "chosen-now"


def test_a_role_its_nodes_ask_for_in_two_modes_is_uncast_by_that_contradiction(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime, ONE_ROLE_IN_TWO_MODES_DOCUMENT)
    headless = publish_configuration(runtime, "opus")
    tool_bearing = publish_configuration(
        runtime, "opus", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    configure_defaults(
        runtime, ((2, "opus", headless),), also_registered=(("opus", tool_bearing),)
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    refused = conductor_start(client, "conductor/two-modes", revision)

    assert refused.status_code == 422, refused.text
    assert refused.json()["uncast_roles"] == [
        {"role": "builder", "reason": "role-modes-conflict"}
    ]
    assert run_ids(runtime.engine) == []


def test_a_mode_gap_ends_the_difficulty_walk_instead_of_being_stepped_over(
    three_provider_runtime: DbosRuntime,
) -> None:
    """No step is reached over a step whose model cannot fill the node's mode."""
    runtime = three_provider_runtime
    revision, _lineage_id = publish_workflow(runtime, EASY_FAMILY_DOCUMENT)
    small = publish_configuration(runtime, "small")
    middle = publish_configuration(
        runtime,
        "middle",
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        provider="openai",
    )
    large = publish_configuration(runtime, "large", provider="anthropic")
    configure_defaults_across_providers(
        runtime,
        (
            (1, "exact", "small", small),
            (2, "openai", "middle", middle),
            (3, "anthropic", "large", large),
        ),
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    resolution = resolve(client, revision)

    assert resolution.status_code == 200, resolution.text
    assert {
        item["role"]: (item["agent_configuration_revision_hash"], item["uncast_reason"])
        for item in resolution.json()["resolutions"]
    } == {
        "builder": (None, "family-difference-unavailable"),
        "reviewer": (small.value, None),
    }


def test_two_configurations_of_a_step_model_in_one_mode_are_named_ambiguous(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    chosen = publish_configuration(runtime, "opus")
    twin = publish_configuration(runtime, "opus", profile_id="second-account")
    configure_defaults(
        runtime, ((2, "opus", chosen),), also_registered=(("opus", twin),)
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    refused = conductor_start(client, "conductor/ambiguous-mode", revision)

    assert refused.status_code == 422, refused.text
    assert refused.json()["uncast_roles"] == [
        {"role": "builder", "reason": "project-default-model-ambiguous"}
    ]
    assert run_ids(runtime.engine) == []


def test_a_role_with_no_default_or_higher_default_is_uncast_and_refused(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime, TWO_ROLE_DOCUMENT)
    configured = publish_configuration(runtime, "opus")
    configure_defaults(runtime, ((2, "opus", configured),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    refused = conductor_start(client, "conductor/half-occupied", revision)

    assert refused.json() == {
        "type": "urn:atelier2:problem:v1:uncast-agent-roles",
        "title": "Agent roles need models",
        "status": 422,
        "detail": "Choose a registered model for every workflow role without one.",
        "uncast_roles": [{"role": "reviewer", "reason": "no-project-default"}],
    }
    assert run_ids(runtime.engine) == []


def test_one_typed_refusal_names_every_uncast_role_and_family_reason(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime, TWO_ROLE_FAMILY_DOCUMENT)
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    refused = conductor_start(client, "conductor/all-uncast", revision)

    assert refused.status_code == 422
    assert refused.json() == {
        "type": "urn:atelier2:problem:v1:uncast-agent-roles",
        "title": "Agent roles need models",
        "status": 422,
        "detail": "Choose a registered model for every workflow role without one.",
        "uncast_roles": [
            {"role": "builder", "reason": "no-project-default"},
            {
                "role": "reviewer",
                "reason": "no-project-default",
                "family_differs_from": "builder",
            },
        ],
    }
    assert run_ids(runtime.engine) == []


def test_resolution_and_start_keep_the_satisfiable_tail_of_a_family_chain(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(
        runtime, THREE_ROLE_PARTIAL_FAMILY_DOCUMENT
    )
    alpha = publish_configuration(runtime, "alpha")
    preferred = publish_configuration(runtime, "preferred")
    channel = DbosHostConfigurationChannel(runtime.engine)
    anthropic = ModelRegistryRevision(
        ProviderId("anthropic"),
        1,
        (
            ModelRegistryEntry(
                "alpha",
                alpha,
                ModelRegistryEntrySource.OPERATOR,
                ProviderModelCheck.CHECKED,
            ),
        ),
    )
    openai = ModelRegistryRevision(
        ProviderId("openai"),
        1,
        (
            ModelRegistryEntry(
                "preferred",
                preferred,
                ModelRegistryEntrySource.OPERATOR,
                ProviderModelCheck.CHECKED,
            ),
        ),
    )
    assert isinstance(
        channel.publish_model_registry_revision(anthropic),
        ModelRegistryRevisionCreated,
    )
    assert isinstance(
        channel.publish_model_registry_revision(openai),
        ModelRegistryRevisionCreated,
    )
    assert isinstance(
        channel.publish_project_model_defaults_revision(
            ProjectModelDefaultsRevision(
                SERVED_PROJECT,
                1,
                (
                    ProjectModelDefault(
                        cast(RoleDifficulty, 2),
                        openai.revision_hash,
                        openai.provider_id,
                        "preferred",
                        preferred,
                    ),
                    ProjectModelDefault(
                        cast(RoleDifficulty, 3),
                        anthropic.revision_hash,
                        anthropic.provider_id,
                        "alpha",
                        alpha,
                    ),
                ),
            )
        ),
        ProjectModelDefaultsRevisionCreated,
    )
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)
    project_reference = encode_public_project_reference(SERVED_PROJECT)

    resolution = client.post(
        PROJECT_MODEL_RESOLUTION_PATH.replace(
            "{public_project_reference}", project_reference
        ),
        json={"workflow_revision_hash": revision.revision_hash.value, "overrides": []},
    )
    refused = conductor_start(client, "conductor/partial-family", revision)

    assert resolution.status_code == 200, resolution.text
    assert {
        item["role"]: item["agent_configuration_revision_hash"]
        for item in resolution.json()["resolutions"]
    } == {"alpha": None, "beta": alpha.value, "gamma": preferred.value}
    assert refused.status_code == 422
    assert refused.json()["uncast_roles"] == [
        {
            "role": "alpha",
            "reason": "family-difference-unavailable",
            "family_differs_from": "beta",
        }
    ]
    assert run_ids(runtime.engine) == []


def test_start_and_resolution_refuse_an_extra_role_before_casting(
    runtime: DbosRuntime,
) -> None:
    revision, _lineage_id = publish_workflow(runtime)
    configured = publish_configuration(runtime, "opus")
    configure_defaults(runtime, ((2, "opus", configured),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)
    project_reference = encode_public_project_reference(SERVED_PROJECT)

    resolution = client.post(
        PROJECT_MODEL_RESOLUTION_PATH.replace(
            "{public_project_reference}", project_reference
        ),
        json={
            "workflow_revision_hash": revision.revision_hash.value,
            "overrides": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": configured.value,
                },
                {
                    "role": "intruder",
                    "agent_configuration_revision_hash": configured.value,
                },
            ],
        },
    )
    started = conductor_start(
        client,
        "conductor/extra-role",
        revision,
        (
            AgentRoleBinding("builder", configured.value),
            AgentRoleBinding("intruder", configured.value),
        ),
    )

    assert resolution.status_code == 422
    assert resolution.json()["type"].endswith(":invalid-agent-bindings")
    assert started.status_code == 422
    assert started.json()["type"].endswith(":invalid-agent-bindings")
    assert run_ids(runtime.engine) == []


def test_an_admitted_queue_item_starts_on_the_projects_default(
    runtime: DbosRuntime,
) -> None:
    """The launch sweep names no binding, so until now it could only wait."""
    _revision, lineage_id = publish_workflow(runtime)
    configured = publish_configuration(runtime, "opus")
    configure_defaults(runtime, ((2, "opus", configured),))
    admit_queue_item(runtime, lineage_id)

    runtime.launch()

    started = run_ids(runtime.engine)
    assert len(started) == 1
    assert stored_bindings(runtime.engine, started[0]) == [
        ("builder", configured.value)
    ]


def test_retrying_a_run_after_the_project_default_changed_conflicts(
    runtime: DbosRuntime,
) -> None:
    """The run keeps the model matrix it started with when defaults move.

    A resolved start pins the binding-set hash the defaults produced. Re-deriving
    the same run id under different defaults is therefore a different run
    asking for an identity that is taken -- which is what the conflict is for,
    rather than answering with a run whose bindings the caller no longer means.
    """
    revision, _lineage_id = publish_workflow(runtime)
    first = publish_configuration(runtime, "opus")
    recast = publish_configuration(runtime, "sonnet")
    configure_defaults(runtime, ((2, "opus", first),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)
    assert conductor_start(client, "conductor/recast", revision).status_code == 201

    configure_defaults(runtime, ((2, "sonnet", recast),), revision_number=2)
    retried = conductor_start(client, "conductor/recast", revision)

    assert retried.json()["type"].endswith("run-identity-conflict"), retried.text
    assert stored_bindings(runtime.engine, "conductor/recast") == [
        ("builder", first.value)
    ]
