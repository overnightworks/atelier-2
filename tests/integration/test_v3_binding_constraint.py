"""A declared `distinct_from` refuses the same occupation at start."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableBindingConstraintRefused,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

TWO_AGENT_DOCUMENT = (
    b"""format_version: 3
name: Review then merge
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write the change.
"""
    + declared_output()
    + b"""  - id: merge
    type: agent
    role: merger
    mode: headless
    instruction: Land the change.
    depends_on: [implement]
    binding_constraint: {distinct_from: implement}
"""
    + declared_output(name="landed")
)

RUN = RunId("v3/distinct-occupation")


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "v3-distinct-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def publish(
    runtime: DbosRuntime,
    *,
    same_occupation: bool,
    document: bytes = TWO_AGENT_DOCUMENT,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    published = DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    first = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    second = (
        first
        if same_occupation
        else AgentConfigurationRevision(
            "sonnet",
            auth.revision_hash,
            AgentExecutorRevision("exact/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(first),
        AgentConfigurationRevisionCreated,
    )
    if second is not first:
        assert isinstance(
            catalog.publish_agent_configuration_revision(second),
            AgentConfigurationRevisionCreated,
        )
    publish_checked_model_registry(runtime.engine, ProviderId("exact"), (first, second))
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (
            AgentBinding(AgentRole("builder"), first.revision_hash),
            AgentBinding(AgentRole("merger"), second.revision_hash),
        )
    )


def starter_of(runtime: DbosRuntime) -> DbosDurableRunStarter:
    return DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    )


@pytest.mark.proves("the-same-occupation-is-refused-before-any-process")
def test_the_same_occupation_is_refused_before_any_row(runtime: DbosRuntime) -> None:
    workflow, bindings = publish(runtime, same_occupation=True)

    result = starter_of(runtime).start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )

    assert result == DurableBindingConstraintRefused("merge", "implement")
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.proves("the-same-occupation-is-refused-before-any-process")
def test_the_public_start_names_both_nodes_when_occupation_collides(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish(runtime, same_occupation=True)

    refused = durable_api_client(runtime).post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 2,
            "run_id": RUN.value,
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
                for binding in bindings.bindings
            ],
        },
    )

    assert refused.status_code == 422
    problem = refused.json()
    assert problem["type"].endswith(":binding-constraint-refused")
    assert "merge" in problem["detail"]
    assert "implement" in problem["detail"]
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.proves(
    "different-occupations-start-and-an-undeclared-constraint-is-silent"
)
def test_different_occupations_start(runtime: DbosRuntime) -> None:
    workflow, bindings = publish(runtime, same_occupation=False)

    result = starter_of(runtime).start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )

    assert isinstance(result, DurableRunCreated)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    assert RunState(str(record["state"])) is RunState.STARTED


@pytest.mark.proves(
    "different-occupations-start-and-an-undeclared-constraint-is-silent"
)
def test_an_undeclared_constraint_starts_on_the_same_occupation(
    runtime: DbosRuntime,
) -> None:
    document = TWO_AGENT_DOCUMENT.replace(
        b"    binding_constraint: {distinct_from: implement}\n", b""
    )
    workflow, bindings = publish(runtime, same_occupation=True, document=document)

    result = starter_of(runtime).start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )

    assert isinstance(result, DurableRunCreated)


@pytest.mark.proves("the-same-occupation-is-refused-before-any-process")
def test_the_occupation_check_is_what_refuses_the_same_binding(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow, bindings = publish(runtime, same_occupation=True)
    monkeypatch.setattr(
        "atelier2.application.resolve_start_bindings._refused_distinct_occupation",
        lambda _graph, _resolved: None,
    )

    result = starter_of(runtime).start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )

    assert isinstance(result, DurableRunCreated)
