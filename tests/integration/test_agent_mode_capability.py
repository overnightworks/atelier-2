"""A run binds each agent node only to a configuration of the node's own mode."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.node_binding_codec import decode_node_binding
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    run_agent_bindings,
    run_events,
    run_forks,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.transactions import keeping_nothing
from atelier2.adapters.dbos.workflow import _node_binding
from atelier2.api.openapi import API_PREFIX
from atelier2.application.bind_node import agent_execution_request_v2
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agent_modes import AgentModeMismatch
from atelier2.contracts.agents import (
    UNATTENDED_AGENT_EXECUTION_CAPABILITIES,
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
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    AgentExecutionRefusal,
    RunEventKind,
)
from atelier2.contracts.node_bindings import AgentNodeBindingV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.workflows_v3 import AgentMode
from atelier2.ports.agent_attempts import AgentAttemptCancellationAccepted
from atelier2.ports.durable_run_forks import ForkRunRequest
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
    agent_scratch_root,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/mode-meets-capability")
MODE_MISMATCH_REFUSAL = AgentExecutionRefusal.AGENT_MODE_MISMATCH.value.encode("ascii")

MISMATCHED = (
    pytest.param(
        "headless",
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        id="headless-node-on-a-tool-bearing-configuration",
    ),
    pytest.param(
        "headless_with_tools",
        AgentExecutionCapability.HEADLESS,
        id="tool-bearing-node-on-a-headless-configuration",
    ),
)
MATCHED = (
    pytest.param("headless", AgentExecutionCapability.HEADLESS, id="headless"),
    pytest.param(
        "headless_with_tools",
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        id="headless-with-tools",
    ),
)


@pytest.fixture
def executor() -> RecordingAgentExecutorFactoryV2:
    return RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        b'"the judgement"',
        capability_set=UNATTENDED_AGENT_EXECUTION_CAPABILITIES,
    )


@pytest.fixture
def runtime(
    tmp_path: Path, executor: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "mode-capability-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (executor,),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def publish(
    runtime: DbosRuntime, mode: AgentMode, capability: AgentExecutionCapability
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """One reviewer node declaring `mode`, and one configuration declaring `capability`."""
    DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog.publish_agent_configuration_revision(configuration)
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    document = (
        b"""format_version: 3
name: One reviewer
nodes:
  - id: review
    type: agent
    role: reviewer
    mode: """
        + mode.encode()
        + b"""
    instruction: Judge the change.
"""
        + declared_output()
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("reviewer"), configuration.revision_hash),)
    )


def starter_of(runtime: DbosRuntime) -> DbosDurableRunStarter:
    return DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    )


def start(
    runtime: DbosRuntime, workflow: WorkflowRevision, bindings: AgentBindingSet
) -> object:
    return starter_of(runtime).start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )


def start_unchecked(
    runtime: DbosRuntime,
    monkeypatch: pytest.MonkeyPatch,
    mode: AgentMode,
    capability: AgentExecutionCapability,
) -> WorkflowRevision:
    """Record a run the way one written before the start's check stands.

    Such a run is durable truth the start can no longer refuse, so its start is
    let through here and what comes after it is asked instead.
    """
    workflow, bindings = publish(runtime, mode, capability)
    with monkeypatch.context() as unchecked:
        unchecked.setattr(
            "atelier2.adapters.dbos.starter.agent_mode_mismatch",
            lambda _graph, _bindings: None,
        )
        assert isinstance(start(runtime, workflow, bindings), DurableRunCreated)
    return workflow


def attempts_of(runtime: DbosRuntime) -> DbosAgentAttemptStore:
    return DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)


def attempt_recorded_before_the_check(
    runtime: DbosRuntime,
    executor: RecordingAgentExecutorFactoryV2,
    workflow: WorkflowRevision,
) -> AgentAttemptExecution:
    """The ordinal-one row a serve wrote for this node before modes were compared."""
    binding = decode_node_binding(
        dict(
            _node_binding(
                runtime.datasource, RUN, workflow.revision_hash, "review", None
            )
        )
    )
    assert isinstance(binding, AgentNodeBindingV2)
    execution = agent_attempt_execution(
        agent_execution_request_v2(
            binding,
            RUN,
            workflow.revision_hash,
            "review",
            executor.operational_identity,
            executor.declared_capabilities,
        )
    )
    attempts_of(runtime).prepare(execution)
    return execution


def rows_of(runtime: DbosRuntime, table: sa.Table) -> int:
    with runtime.engine.connect() as connection:
        return connection.execute(
            sa.select(sa.func.count()).select_from(table)
        ).scalar_one()


def refusals_of(runtime: DbosRuntime) -> list[bytes]:
    """Every agent failure this run carries that names no attempt of its own."""
    with runtime.engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(run_events.c.payload).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
                    run_events.c.agent_attempt_id.is_(None),
                )
            ).scalars()
        )


def state_of(runtime: DbosRuntime) -> RunState:
    with runtime.engine.connect() as connection:
        return RunState(
            str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        )


@pytest.mark.parametrize(("mode", "capability"), MISMATCHED)
def test_a_node_bound_outside_its_mode_is_refused_before_any_run_is_written(
    runtime: DbosRuntime, mode: AgentMode, capability: AgentExecutionCapability
) -> None:
    workflow, bindings = publish(runtime, mode, capability)

    result = start(runtime, workflow, bindings)

    assert result == AgentModeMismatch("review", mode, capability)
    assert rows_of(runtime, runs) == 0


@pytest.mark.parametrize(("mode", "capability"), MATCHED)
def test_a_node_bound_to_its_own_mode_starts_unchanged(
    runtime: DbosRuntime, mode: AgentMode, capability: AgentExecutionCapability
) -> None:
    workflow, bindings = publish(runtime, mode, capability)

    result = start(runtime, workflow, bindings)

    assert isinstance(result, DurableRunCreated)
    assert state_of(runtime) is RunState.STARTED


def test_the_public_start_names_node_mode_and_capability_when_they_disagree(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish(
        runtime, "headless", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )

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
    assert problem["type"].endswith(":agent-mode-mismatch")
    assert "'review'" in problem["detail"]
    assert "headless" in problem["detail"]
    assert "headless_with_tools" in problem["detail"]
    assert rows_of(runtime, runs) == 0


def test_a_stored_run_bound_outside_its_mode_starts_no_process_on_its_next_attempt(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_unchecked(
        runtime, monkeypatch, "headless", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )

    runtime.launch()
    wait_for_run_state(runtime.engine, RUN, RunState.FAILED)

    assert rows_of(runtime, agent_attempts) == 0
    assert refusals_of(runtime) == [MODE_MISMATCH_REFUSAL]


def test_a_replacement_of_a_node_bound_outside_its_mode_is_refused_before_its_row(
    runtime: DbosRuntime,
    executor: RecordingAgentExecutorFactoryV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempt a cancellation would hand the node next is never written."""
    workflow = start_unchecked(
        runtime, monkeypatch, "headless", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    attempts = attempts_of(runtime)
    execution = attempt_recorded_before_the_check(runtime, executor, workflow)
    replace = CancelAgentAttemptRequest(
        RUN,
        execution.attempt_id,
        "the-operator-asks-for-another-try",
        attempts.load(execution.attempt_id).state_version,
        AgentAttemptReplacement.ONE,
    )
    assert isinstance(
        attempts.request_cancellation(replace), AgentAttemptCancellationAccepted
    )

    attested = attempts.attest_cancellation_cleanup(
        replace, AgentAttemptCancellationDisposition.NEVER_LAUNCHED, None, None
    )

    assert attested.replacement_attempt_id is None
    assert rows_of(runtime, agent_attempts) == 1
    assert refusals_of(runtime) == [MODE_MISMATCH_REFUSAL]
    assert state_of(runtime) is RunState.FAILED


def test_a_fork_refuses_an_origin_whose_node_is_bound_outside_its_mode(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successor inherits its origin's bindings, and answers to the same check."""
    start_unchecked(
        runtime, monkeypatch, "headless", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    runtime.launch()
    wait_for_run_state(runtime.engine, RUN, RunState.FAILED)

    refused = starter_of(runtime).fork_run(ForkRunRequest(RUN, "refork", "review"))

    assert refused == AgentModeMismatch(
        "review", "headless", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )
    assert rows_of(runtime, run_forks) == 0
    assert rows_of(runtime, runs) == 1


def test_a_start_judged_over_a_store_that_keeps_nothing_answers_and_keeps_nothing(
    runtime: DbosRuntime,
) -> None:
    """The queue's judge is the real start, and none of what it wrote survives."""
    workflow, bindings = publish(runtime, "headless", AgentExecutionCapability.HEADLESS)
    request = StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    judge = DbosDurableRunStarter(
        keeping_nothing(runtime.engine),
        runtime.settings,
        runtime.agent_executor_registry,
    )

    judged = judge.start_published(request)

    assert isinstance(judged, DurableRunCreated)
    assert rows_of(runtime, runs) == 0
    assert rows_of(runtime, run_agent_bindings) == 0
    assert isinstance(starter_of(runtime).start_published(request), DurableRunCreated)
