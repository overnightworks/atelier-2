"""A V3 agent node reaches the durable attempt path.

H1c's first bound question: today the attempt store refuses a V3 run at its own
front door -- `_validate_request` accepts only a `RunV2` with a
`WorkflowGraphV2`, so a V3 agent node cannot even be prepared, let alone run.

The receipt chain Codex designed on top of this door --

    AgentReceiptV2  (the provider's truth, unchanged)
      -> NodeReceipt.agent_receipt_hash  (node-receipt/v3, Cut B's form)
        -> RunEvent.node_receipt_hash
          -> terminal_hash               (unchanged, #110's chain)

-- is not written here, and the reason is a measured absence rather than a
choice: a `NodeReceipt` names a `node-execution-request/v3` hash and a
`context-package/v3` hash, and no production code authors either record. ADR 0006
binds the manifest to material written once before START, and the request binds
the run configuration revision `RunV3` already documents as unreconstructed. The
attempt path holds an `AgentExecutionRequestV2`, whose hash is framed under a
different domain, so linking it in would publish a receipt whose own request hash
recomputes to nothing. The chain waits on that author, not on this door.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.node_binding_codec import EncodedAgentBindingV2
from atelier2.adapters.dbos.run_store import (
    RunTransitionConflict,
    load_graph,
    load_run_inputs,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import run_events, runs
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.dbos.workflow import _node_binding
from atelier2.application.compose_node_job import node_job
from atelier2.application.project_node_rail import NodeRailAttempt, project_node_rail
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.process_endings import ProcessExitSignature
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows import RunCompletes, RunContinues
from atelier2.contracts.workflows_v3 import AgentNodeV3
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.run_queries import NodeDetailFound, RunFound
from tests.integration.test_v3_agent_start import publish
from tests.integration.test_v3_ordered_run import order, publish_ordered_workflow, start
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.workflows import declared_output

RUN = RunId("v3/attempt")
ORDERED_RUN = RunId("v3/ordered-query")
CURRENT_STRING_ORDERED_RUN = RunId("v3/current-string-ordered-query")
INSTRUCTION = b"Do the one thing this chain is for."
PROVIDER_OUTPUT = b'"the exact provider bytes"'
"""One JSON value, because every executable agent node declares a schema for one."""

STRING_ORDER_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type":"string"}')


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        canonical_runtime_settings(tmp_path, "h1c-test", agent_scratch_root(tmp_path)),
        canonical_loopback_effects(tmp_path),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def started_v3_attempt(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentAttemptExecution]:
    """One started V3 run, and the attempt its agent node would run under."""
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    binding = run.agent_bindings[0]
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(RUN, revision_hash, "implement"),
        RUN,
        revision_hash,
        "implement",
        ResolvedAgentBinding(binding.role, binding.configuration, binding.auth_profile),
        AgentExecutorOperationalIdentity("exact-operation"),
        INSTRUCTION,
    )
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    return workflow, execution


def started_ordered_v3_attempt(runtime: DbosRuntime) -> AgentAttemptExecution:
    """One started V3 run that carries an order, and the attempt its cook would run.

    The stored request hash is taken over the composed job the attempt store
    already uses. The query must recompute that same job; the instruction alone
    is a different identity.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    created = start(runtime, workflow, bindings, ORDERED_RUN, order(b'{"portions": 4}'))
    assert isinstance(created, DurableRunCreated)
    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(
                sa.select(runs).where(runs.c.run_id == ORDERED_RUN.value)
            )
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
        assert isinstance(run, RunV3)
        graph = load_graph(connection, run.revision_hash)
        node = graph.node(run.current_node_id)
        assert isinstance(node, AgentNodeV3)
        authored_job = node_job(
            node.instruction, load_run_inputs(connection, run.run_id, node)
        ).encode("utf-8")
        binding = run.agent_bindings[0]
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(ORDERED_RUN, revision_hash, run.current_node_id),
        ORDERED_RUN,
        revision_hash,
        run.current_node_id,
        ResolvedAgentBinding(binding.role, binding.configuration, binding.auth_profile),
        AgentExecutorOperationalIdentity("exact-operation"),
        authored_job,
    )
    return AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )


def started_string_ordered_v3_attempt(
    runtime: DbosRuntime, run_id: RunId
) -> AgentAttemptExecution:
    """The request one raw declared string order prepares (#1091)."""
    workflow, bindings = publish_ordered_workflow(runtime, STRING_ORDER_SCHEMA)
    created = start(
        runtime,
        workflow,
        bindings,
        run_id,
        order(b"the authored string order", STRING_ORDER_SCHEMA),
    )
    assert isinstance(created, DurableRunCreated)
    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
        assert isinstance(run, RunV3)
        graph = load_graph(connection, run.revision_hash)
        node = graph.node(run.current_node_id)
        assert isinstance(node, AgentNodeV3)
        orders = load_run_inputs(connection, run.run_id, node)
        binding = run.agent_bindings[0]

    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, run.current_node_id),
        run_id,
        revision_hash,
        run.current_node_id,
        ResolvedAgentBinding(binding.role, binding.configuration, binding.auth_profile),
        AgentExecutorOperationalIdentity("exact-operation"),
        node_job(node.instruction, orders).encode("utf-8"),
    )
    return AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )


def test_get_run_attaches_a_prepared_v3_attempt(runtime: DbosRuntime) -> None:
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)

    found = durable_queries(runtime.engine).get_run(RUN)

    assert isinstance(found, RunFound)
    attempt = found.projection.current_agent_attempt
    assert attempt is not None
    assert attempt.attempt_ordinal == 1
    assert attempt.state is PublicAgentAttemptState.PREPARED


def test_get_run_attaches_a_prepared_v3_attempt_that_carries_an_order(
    runtime: DbosRuntime,
) -> None:
    """A stored hash includes the order; the query must recompute it the same way."""
    execution = started_ordered_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)

    found = durable_queries(runtime.engine).get_run(ORDERED_RUN)

    assert isinstance(found, RunFound)
    attempt = found.projection.current_agent_attempt
    assert attempt is not None
    assert attempt.attempt_ordinal == 1
    assert attempt.state is PublicAgentAttemptState.PREPARED


def test_a_prepared_string_order_attempt_projects_under_its_hashed_composition(
    runtime: DbosRuntime,
) -> None:
    execution = started_string_ordered_v3_attempt(runtime, CURRENT_STRING_ORDERED_RUN)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)

    found = durable_queries(runtime.engine).get_run(CURRENT_STRING_ORDERED_RUN)

    assert isinstance(found, RunFound)
    attempt = found.projection.current_agent_attempt
    assert attempt is not None
    assert attempt.request_hash == execution.request.request_hash
    detail = durable_queries(runtime.engine).get_node_detail(
        CURRENT_STRING_ORDERED_RUN, "cook"
    )
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.WORKING


def test_get_run_answers_a_v3_agent_run_with_no_attempt_rows(
    runtime: DbosRuntime,
) -> None:
    started_v3_attempt(runtime)

    found = durable_queries(runtime.engine).get_run(RUN)

    assert isinstance(found, RunFound)
    assert found.projection.current_agent_attempt is None
    assert found.projection.agent_attempts == ()


def test_get_run_answers_a_completed_v3_sink_without_a_current_attempt(
    runtime: DbosRuntime,
) -> None:
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)
    store.complete_success(execution, AgentExecutionResult(PROVIDER_OUTPUT))

    found = durable_queries(runtime.engine).get_run(RUN)

    assert isinstance(found, RunFound)
    assert found.projection.run.state is RunState.COMPLETED
    assert found.projection.current_agent_attempt is None


def test_get_run_answers_a_failed_v3_node_with_its_current_attempt(
    runtime: DbosRuntime,
) -> None:
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)
    store.complete_known_failure(execution, ProcessExitSignature(1, b"failed"))

    found = durable_queries(runtime.engine).get_run(RUN)
    assert isinstance(found, RunFound)
    rail = project_node_rail(found.projection, ())

    assert found.projection.run.state is RunState.FAILED
    assert found.projection.current_agent_attempt is not None
    assert (
        found.projection.current_agent_attempt.state is PublicAgentAttemptState.FAILED
    )
    assert rail[0].state is NodeState.FAILED
    assert rail[0].attempt == NodeRailAttempt(1, PublicAgentAttemptState.FAILED)


@pytest.mark.proves("a-v3-agent-node-reaches-the-durable-attempt-path")
def test_a_v3_attempt_is_admitted_by_the_attempt_store(runtime: DbosRuntime) -> None:
    """The door that refuses today: 'agent attempt requires a V2 run'."""
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    prepared = store.prepare(execution)

    assert prepared.node_id == "implement"
    assert prepared.attempt_ordinal == 1


@pytest.mark.proves("a-v3-agent-node-reaches-the-durable-attempt-path")
def test_an_attempt_for_a_run_that_does_not_exist_is_still_refused(
    runtime: DbosRuntime,
) -> None:
    """Widening the door must not open it: an absent run is still no run."""
    _workflow, execution = started_v3_attempt(runtime)
    absent = RunId("v3/absent")
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(
            absent, execution.request.workflow_revision_hash, "implement"
        ),
        absent,
        execution.request.workflow_revision_hash,
        "implement",
        execution.request.resolved_binding,
        execution.request.executor_operational_identity,
        INSTRUCTION,
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    with pytest.raises(RunTransitionConflict):
        store.prepare(
            AgentAttemptExecution(
                request,
                AgentAttemptId.for_execution(
                    request.node_execution_id, request.request_hash
                ),
                1,
            )
        )


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_completed_v3_attempt_carries_its_run_to_the_terminal_hash(
    runtime: DbosRuntime,
) -> None:
    """The provider's receipt and the run's terminal hash, from one V3 attempt."""
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)

    succeeded = store.complete_success(execution, AgentExecutionResult(PROVIDER_OUTPUT))

    assert isinstance(succeeded, AgentAttemptSucceeded)
    assert succeeded.attempt.state is AgentAttemptState.SUCCEEDED
    assert succeeded.completion == RunCompletes()
    with runtime.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    assert run["state"] == RunState.COMPLETED.value
    assert run["terminal_hash"] is not None


def test_a_live_v3_run_with_a_running_attempt_names_it_on_the_rail(
    runtime: DbosRuntime,
) -> None:
    """The cockpit path, not a hand-built projection.

    The rail already knew the V3 attempt vocabulary. get_run still collected
    only AgentNodeV2, so a real V3 run arrived with node states and no attempt.
    """
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)

    found = durable_queries(runtime.engine).get_run(RUN)

    assert isinstance(found, RunFound)
    rail = project_node_rail(found.projection, ())
    assert rail[0].node_id == "implement"
    assert rail[0].attempt == NodeRailAttempt(1, PublicAgentAttemptState.POSSIBLY_RAN)


LINE_DOCUMENT = (
    b"""format_version: 3
name: A line of two agents
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the first thing this chain is for.
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [implement]
"""
    + declared_output()
)
LINE_ROLES = (
    ("implement", "builder", b"Do the first thing this chain is for."),
    ("review", "reviewer", b"Judge the first thing."),
)


def _durable_head(runtime: DbosRuntime) -> tuple[str, str]:
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    return str(record["current_node_id"]), str(record["state"])


def _attempt_for(
    runtime: DbosRuntime,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    role: str,
    instruction: bytes,
) -> AgentAttemptExecution:
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    binding = next(
        candidate for candidate in run.agent_bindings if candidate.role.value == role
    )
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(RUN, revision_hash, node_id),
        RUN,
        revision_hash,
        node_id,
        ResolvedAgentBinding(binding.role, binding.configuration, binding.auth_profile),
        AgentExecutorOperationalIdentity("exact-operation"),
        instruction,
    )
    return AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
@pytest.mark.proves("a-v3-run-follows-the-edge-its-author-declared")
def test_a_succeeded_non_sink_leaves_the_run_standing_on_its_declared_heir(
    runtime: DbosRuntime,
) -> None:
    """The edge, driven through the durable store rather than read off the graph.

    The rule that picks the heir has unit proof; what had none is that a run
    actually arrives there -- that the head the next attempt reads is the node
    the author declared, and that the line ends where its sink does.
    """
    workflow, bindings = publish(runtime, LINE_DOCUMENT, ("builder", "reviewer"))
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    assert _durable_head(runtime) == ("implement", RunState.STARTED.value)

    completions = []
    for node_id, role, instruction in LINE_ROLES:
        execution = _attempt_for(runtime, revision_hash, node_id, role, instruction)
        store.prepare(execution)
        store.claim(execution)
        succeeded = store.complete_success(
            execution, AgentExecutionResult(f'"bytes from {node_id}"'.encode())
        )
        assert isinstance(succeeded, AgentAttemptSucceeded)
        completions.append(succeeded.completion)
        if node_id == "implement":
            # The heir, read back from the durable head the next attempt uses --
            # and bound with its own role, which it could not be before the run
            # arrived: a node binding answers only for the node the run stands on.
            assert _durable_head(runtime) == ("review", RunState.STARTED.value)
            heir = cast(
                EncodedAgentBindingV2,
                _node_binding(
                    runtime.datasource, RUN, workflow.revision_hash, "review", None
                ),
            )
            assert heir["role"] == "reviewer"
            assert heir["job"] == "Judge the first thing."

    assert completions == [RunContinues("review"), RunCompletes()]
    assert _durable_head(runtime) == ("review", RunState.COMPLETED.value)
    with runtime.engine.connect() as connection:
        events = [
            (int(row["event_sequence"]), str(row["node_id"]))
            for row in connection.execute(
                sa.select(run_events).order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        terminal = connection.scalar(
            sa.select(runs.c.terminal_hash).where(runs.c.run_id == RUN.value)
        )

    assert events == [(1, "implement"), (2, "review")]
    assert terminal is not None
