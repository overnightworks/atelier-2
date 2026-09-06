from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from atelier2.adapters.dbos import run_transitions as run_transitions_module
from atelier2.adapters.dbos import starter as starter_module
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.queries import DbosQueries, WaitAnswerProjectionCorrupt
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    WaitAnswerStateCorrupt,
    commit_wait_answered,
    load_wait_answer,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    commit_reconciliation_required,
    commit_waiting_input,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_events,
    runs,
    wait_answers,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import (
    answer_workflow_id_for,
    bootstrap_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.api.app import create_app
from atelier2.api.limits import ApiLimits, durable_projection_limit
from atelier2.api.references import encode_canonical_base64, encode_public_run_reference
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agents import (
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    SubmitWaitAnswerRequest,
    TransitionSnapshot,
    WaitAnswerActor,
    WaitAnswerAttributionKind,
    WaitAnswerState,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import AnyRun, RunV3
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.agent_executions import AgentExecutorKey, AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
)
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCreated,
    DurableRevisionExisting,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_scratch_root,
    failing_agent_executor_factory,
)
from tests.scenarios.api import (
    api_limits,
    durable_ports,
    event_poll_backoff,
)
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    NO_AGENT_EXECUTORS,
    V3_EXECUTOR_REVISION,
    V3_PROVIDER,
    complete_v3_agent_node,
    prepare_and_launch_graph_action,
    start_published_v3_run,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, V3_DOCUMENT, declared_output


def _wait_sink_document(prompt: str) -> bytes:
    """A served line whose single node is the answer a person owes it.

    It declares no role, so a start binds no agent and needs no executor: what
    the HTTP tests below drive is publication, start, pagination and the answer,
    and none of those is about who runs a node.
    """

    return (
        f"""format_version: 3
name: One answer ends this run
nodes:
  - id: wait
    type: wait
    prompt: {prompt}
""".encode()
        + declared_output(ANY_JSON_SCHEMA, "answer")
    )


WAIT_SINK_DOCUMENT = _wait_sink_document("What is the answer?")
OPEN_PR_OPERATION = PublishedRevision(
    RevisionKind.ADAPTER_OPERATION,
    json.dumps({"operation": AdapterOperationName.OPEN_PR.value}).encode("utf-8"),
)
ACTION_INSTRUCTION = "Compose the request the action lands."
ACTION_AGENT_OUTPUT = b'"request"'
ACTION_DOCUMENT = (
    f"""format_version: 3
name: One agent, one action, one answer
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: {ACTION_INSTRUCTION}
""".encode()
    + declared_output(ANY_JSON_SCHEMA, "request")
    + f"""  - id: action
    type: action
    operation: {{ref: open-pr, revision: {OPEN_PR_OPERATION.revision_hash.value}}}
    depends_on: [agent]
    inputs:
      - name: body
        from: {{node: agent, output: request}}
  - id: wait
    type: wait
    prompt: Did the landing hold?
    depends_on: [action]
""".encode()
    + declared_output(ANY_JSON_SCHEMA, "answer")
)
PAUSED_LINE_INSTRUCTION = "Say the thing a person will judge."
PAUSED_LINE_AGENT_OUTPUT = b'"payload"'
PAUSED_LINE_DOCUMENT = (
    f"""format_version: 3
name: One agent, then one answer
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: {PAUSED_LINE_INSTRUCTION}
""".encode()
    + declared_output()
    + b"""  - id: wait
    type: wait
    prompt: Did the agent's work hold?
    depends_on: [agent]
"""
    + declared_output(ANY_JSON_SCHEMA, "answer")
)
"""The format-3 mirror of the retired format-1 store scaffolding: agent, wait.

The wait-answer machinery below needs a run resting at a Wait whose document
also declares a non-wait node, so the corruption cases can point the head at
one. The agent's success goes through the real attempt door; the pause is
committed directly, because no runtime drives these runs.
"""

RECONCILED_LOGICAL_KEY = (
    "atelier2-node-effect-"
    "0e5d06176a9f5c2a7bfbdfb27107b55ec64252e54629a05030bc6da01b4a8a1a"
)
RECONCILED_REVISION_HASH = (
    "d3a30e9679b8e78e212aacd77993083330561cecae9ca7592e3e85c232bb05e5"
)
RECONCILED_REQUEST = (
    b'{"body":"\\"request\\"","head_branch":"atelier2-open-pr-8869ad22943e"}'
)
RECONCILED_REQUEST_HASH = (
    "8233a24c4c2c25362c76b8f841b8b5efc8f2d7edf196b748c92827601375c2d0"
)
RECONCILED_APPLIED_RESULT_HASH = (
    "fbf5b216105e471c4f89e92a1ec12897ee9f2b439eb200a4f7855901d2889e7e"
)
"""What the reconciled line mints, written out rather than recomputed.

Asking the intent for the hash it already carries compares the store with the
object that wrote it, and moves with any change to either. These are the whole
literal forms, so a changed digest, separator or encoding turns identical work
into a second identity loudly (acceptance/88) instead of silently agreeing with
itself. They follow ACTION_DOCUMENT: change that document and these change with
it, which is the point at which a reader has to look.
"""


# A contended write is refused without waiting at all: the pool fails a checkout
# it cannot serve at once, and the driver fails a write against a held lock on
# its first attempt. Zero is what makes the refusal a decision rather than an
# elapsed measurement, so the test needs no clock to say which bound governed.
NO_WAIT_FOR_A_CONTENDED_WRITE = 0.0


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    configured = DbosRuntime(
        canonical_runtime_settings(tmp_path, "api-tests", agent_scratch_root(tmp_path)),
        canonical_loopback_effects(tmp_path),
        (failing_agent_executor_factory(V3_PROVIDER.value, []),),
    )
    configured.initialize_storage()
    try:
        yield configured
    finally:
        configured.close()


@dataclass(frozen=True)
class DurableSnapshot:
    tables: tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]
    workflow_count: int


def _durable_snapshot(runtime: DbosRuntime) -> DurableSnapshot:
    tables = (
        workflow_revisions,
        runs,
        effect_intents,
        reconcile_commands,
        effect_receipts,
        run_events,
        wait_answers,
    )
    with runtime.engine.connect() as connection:
        contents = tuple(
            (
                table.name,
                tuple(
                    sorted(
                        (
                            tuple(record)
                            for record in connection.execute(sa.select(table))
                        ),
                        key=repr,
                    )
                ),
            )
            for table in tables
        )
        workflow_count = int(
            connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_status")) or 0
        )
    return DurableSnapshot(contents, workflow_count)


def _publish_referenced_revisions(
    runtime: DbosRuntime, *revisions: PublishedRevision
) -> None:
    """Publish what a served document points at, before the document names it."""

    catalog = DbosCatalogStore(runtime.engine)
    for revision in revisions:
        published = catalog.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published


def _start_body(
    run_id: RunId, revision_hash: WorkflowRevisionHash
) -> dict[str, object]:
    """What the wire asks for to start a format-3 run that binds no role."""

    return {
        "workflow_format_version": 3,
        "run_id": run_id.value,
        "workflow_revision_hash": revision_hash.value,
        "agent_bindings": [],
        "orders": [],
    }


def _pause_for_an_answer(
    runtime: DbosRuntime, client: TestClient, run_id: RunId
) -> TransitionSnapshot:
    """Publish and start the wait-sink line, then pause it where a person answers.

    Publication and the start go through the served routes; only the pause is
    committed directly, because no runtime drives these runs.
    """

    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    published = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=WAIT_SINK_DOCUMENT,
        headers={"content-type": "application/yaml"},
    )
    assert published.status_code in {200, 201}, published.text
    started = client.post(
        "/atelier/api/v1/runs", json=_start_body(run_id, revision.revision_hash)
    )
    assert started.status_code == 201, started.text
    with canonical_write_transaction(runtime.engine) as connection:
        return commit_waiting_input(connection, run_id, revision.revision_hash, "wait")


def _executor_operational_identity(
    registry: AgentExecutorRegistry,
) -> AgentExecutorOperationalIdentity:
    """Which running executor the V3 agent bindings resolve to."""

    key = AgentExecutorKey(V3_PROVIDER, V3_EXECUTOR_REVISION)
    return next(
        entry.operational_identity for entry in registry.manifest if entry.key == key
    )


def _complete_the_action_predecessor(runtime: DbosRuntime, run: AnyRun) -> None:
    """Carry the action line's agent node to its durable success.

    The attempt is the only door that writes a format-3 agent's output, and the
    action that follows it reads exactly that output before it can be prepared.
    """

    assert isinstance(run, RunV3)
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run.run_id, run.revision_hash, "agent"),
        run.run_id,
        run.revision_hash,
        "agent",
        run.agent_bindings[0],
        _executor_operational_identity(runtime.agent_executor_registry),
        ACTION_INSTRUCTION.encode("utf-8"),
    )
    execution = agent_attempt_execution(request)
    attempts = DbosAgentAttemptStore(
        runtime.engine, runtime.settings.application_version
    )
    attempts.prepare(execution)
    attempts.claim(execution)
    succeeded = attempts.complete_success(
        execution, AgentExecutionResult(ACTION_AGENT_OUTPUT)
    )
    assert isinstance(succeeded, AgentAttemptSucceeded), succeeded


def _paused_wait_line(
    runtime: DbosRuntime, run_id: RunId
) -> tuple[WorkflowRevision, TransitionSnapshot]:
    """The paused line's run rested at its Wait, at store level.

    The agent's success goes through the real attempt door; the pause is
    committed directly, because what the callers measure is the answer
    machinery, never a driving runtime.
    """

    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    revision = WorkflowRevision(PAUSED_LINE_DOCUMENT)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        run_id,
        revision,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        run_id,
        "agent",
        PAUSED_LINE_INSTRUCTION.encode("utf-8"),
        PAUSED_LINE_AGENT_OUTPUT,
    )
    with canonical_write_transaction(runtime.engine) as connection:
        waiting = commit_waiting_input(
            connection, run_id, revision.revision_hash, "wait"
        )
    return revision, waiting


def test_revision_publication_created_and_existing_are_decided_by_one_write(
    runtime: DbosRuntime,
) -> None:
    publisher = DbosWorkflowRevisionPublisher(runtime.engine)
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)

    first = publisher.publish(revision)
    retry = publisher.publish(revision)

    assert first == DurableRevisionCreated(revision)
    assert retry == DurableRevisionExisting(revision)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(workflow_revisions)
            )
            == 1
        )


def test_concurrent_publication_has_one_created_fact(runtime: DbosRuntime) -> None:
    publisher = DbosWorkflowRevisionPublisher(runtime.engine)
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    barrier = Barrier(4)

    def publish(_: int) -> object:
        barrier.wait(timeout=5)
        return publisher.publish(revision)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(publish, range(4)))

    assert sum(isinstance(result, DurableRevisionCreated) for result in results) == 1
    assert sum(isinstance(result, DurableRevisionExisting) for result in results) == 3


@pytest.mark.parametrize("contention", ["pool", "writer-lock"])
def test_every_typed_writer_maps_connection_contention_to_unavailable(
    runtime: DbosRuntime, contention: str
) -> None:
    intent = _waiting_reconciliation(runtime)
    revision = WorkflowRevision(ACTION_DOCUMENT)
    configured = sa.create_engine(
        runtime.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=NO_WAIT_FOR_A_CONTENDED_WRITE,
        connect_args={
            "check_same_thread": False,
            "timeout": NO_WAIT_FOR_A_CONTENDED_WRITE,
        },
    )
    operations = (
        lambda: DbosWorkflowRevisionPublisher(configured).publish(revision),
        lambda: DbosDurableRunStarter(
            configured,
            runtime.settings,
            NO_AGENT_EXECUTORS,
        ).start_published(
            StartPublishedRunRequest(RunId("contended-start"), revision.revision_hash)
        ),
        lambda: DbosWaitAnswerer(
            configured, runtime.settings.application_version
        ).submit_result(
            SubmitWaitAnswerRequest(
                intent.binding.run_id,
                intent.binding.workflow_revision_hash,
                "wait",
                NodeExecutionId.for_node(
                    intent.binding.run_id,
                    intent.binding.workflow_revision_hash,
                    "wait",
                ),
                WaitAnswerActor.OPERATOR,
                b"17",
            )
        ),
        lambda: DbosEffectReconcileCommander(
            configured, runtime.settings
        ).submit_result(_command(intent)),
    )
    try:
        if contention == "pool":
            with configured.connect():
                results = tuple(operation() for operation in operations)
        else:
            with runtime.engine.connect() as lock_owner:
                lock_owner.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    results = tuple(operation() for operation in operations)
                finally:
                    lock_owner.rollback()
    finally:
        configured.dispose()

    assert all(isinstance(result, DurableWriteUnavailable) for result in results)


def test_missing_revision_is_a_typed_in_transaction_start_result(
    runtime: DbosRuntime,
) -> None:
    starter = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        NO_AGENT_EXECUTORS,
    )

    result = starter.start_published(
        StartPublishedRunRequest(
            RunId("missing-parent"), WorkflowRevisionHash("0" * 64)
        )
    )

    assert isinstance(result, DurableRunRevisionMissing)
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


def test_missing_revision_start_never_acquires_a_write_lock_or_blocks_publication(
    runtime: DbosRuntime,
) -> None:
    requested = WorkflowRevisionHash("0" * 64)
    concurrent_revision = WorkflowRevision(
        WAIT_SINK_DOCUMENT.replace(
            b"What is the answer?", b"What is a different answer?"
        )
    )
    statements: list[str] = []
    publication_completed = False
    publication_in_progress = False

    def publish_while_missing_read_connection_is_open(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal publication_completed, publication_in_progress
        if publication_in_progress:
            return
        statements.append(statement)
        if publication_completed or "workflow_revisions.document" not in statement:
            return
        publication_in_progress = True
        try:
            publication_completed = isinstance(
                DbosWorkflowRevisionPublisher(runtime.engine).publish(
                    concurrent_revision
                ),
                DurableRevisionCreated,
            )
        finally:
            publication_in_progress = False

    event.listen(
        runtime.engine,
        "after_cursor_execute",
        publish_while_missing_read_connection_is_open,
    )
    try:
        result = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            NO_AGENT_EXECUTORS,
        ).start_published(StartPublishedRunRequest(RunId("missing-lock"), requested))
    finally:
        event.remove(
            runtime.engine,
            "after_cursor_execute",
            publish_while_missing_read_connection_is_open,
        )

    assert isinstance(result, DurableRunRevisionMissing)
    assert publication_completed
    assert all("BEGIN IMMEDIATE" not in statement.upper() for statement in statements)


def test_concurrent_start_enqueues_only_the_transaction_that_created_the_run(
    runtime: DbosRuntime,
) -> None:
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    assert isinstance(
        DbosWorkflowRevisionPublisher(runtime.engine).publish(revision),
        DurableRevisionCreated,
    )
    starter = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        NO_AGENT_EXECUTORS,
    )
    request = StartPublishedRunRequest(
        RunId("concurrent/start"), revision.revision_hash
    )
    barrier = Barrier(4)

    def start(_: int) -> object:
        barrier.wait(timeout=5)
        return starter.start_published(request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(start, range(4)))

    assert sum(isinstance(result, DurableRunCreated) for result in results) == 1
    assert sum(isinstance(result, DurableRunExisting) for result in results) == 3
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": bootstrap_workflow_id_for(request.run_id)},
            )
            == 1
        )


def test_concurrent_wait_answer_enqueues_only_the_created_answer(
    runtime: DbosRuntime,
) -> None:
    revision, _waiting = _paused_wait_line(runtime, RunId("wait-run"))
    answerer = DbosWaitAnswerer(runtime.engine, runtime.settings.application_version)
    request = SubmitWaitAnswerRequest(
        RunId("wait-run"),
        revision.revision_hash,
        "wait",
        NodeExecutionId.for_node(RunId("wait-run"), revision.revision_hash, "wait"),
        WaitAnswerActor.OPERATOR,
        b"17",
    )
    barrier = Barrier(4)

    def answer(_: int) -> object:
        barrier.wait(timeout=5)
        return answerer.submit_result(request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(answer, range(4)))

    assert sum(isinstance(result, DurableAnswerCreated) for result in results) == 1
    assert sum(isinstance(result, DurableAnswerExisting) for result in results) == 3
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(wait_answers)) == 1
        )


def test_concurrent_http_retries_create_one_revision_run_answer_and_workflow(
    runtime: DbosRuntime,
) -> None:
    parallelism = 4
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    publication_barrier = Barrier(parallelism)

    def publish(_: int) -> int:
        publication_barrier.wait(timeout=5)
        return (
            _client(runtime)
            .post(
                "/atelier/api/v1/workflow-revisions",
                content=WAIT_SINK_DOCUMENT,
                headers={"content-type": "application/yaml"},
            )
            .status_code
        )

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        publication_statuses = list(pool.map(publish, range(parallelism)))

    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    assert publication_statuses.count(201) == 1
    assert publication_statuses.count(200) == parallelism - 1

    start_run_id = RunId("http-concurrent-start")
    start_barrier = Barrier(parallelism)

    def start(_: int) -> int:
        start_barrier.wait(timeout=5)
        return (
            _client(runtime)
            .post(
                "/atelier/api/v1/runs",
                json=_start_body(start_run_id, revision.revision_hash),
            )
            .status_code
        )

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        start_statuses = list(pool.map(start, range(parallelism)))

    assert start_statuses.count(201) == 1
    assert start_statuses.count(200) == parallelism - 1

    answer_run_id = RunId("http-concurrent-answer")
    waiting = _pause_for_an_answer(runtime, _client(runtime), answer_run_id)
    answer_path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(answer_run_id)}/answers"
    )
    answer_body = {
        "workflow_revision_hash": revision.revision_hash.value,
        "node_id": "wait",
        "expected_node_execution_id": waiting.event.node_execution_id.value,
        "actor": "operator",
        "answer_base64": encode_canonical_base64(b"17"),
    }
    answer_barrier = Barrier(parallelism)

    def answer(_: int) -> tuple[int, str | None]:
        answer_barrier.wait(timeout=5)
        response = _client(runtime).post(answer_path, json=answer_body)
        problem_type = (
            response.json().get("type") if response.status_code == 409 else None
        )
        return response.status_code, problem_type

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        answer_results = list(pool.map(answer, range(parallelism)))

    assert {status for status, _ in answer_results} <= {202, 409}
    assert all(
        problem_type is None or problem_type.endswith(":answer-execution-stale")
        for _, problem_type in answer_results
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(workflow_revisions)
            )
            == 1
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 2
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(wait_answers)) == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": bootstrap_workflow_id_for(start_run_id)},
            )
            == 1
        )
        wait_execution = NodeExecutionId.for_node(
            answer_run_id, revision.revision_hash, "wait"
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": answer_workflow_id_for(wait_execution)},
            )
            == 1
        )


def _waiting_reconciliation(runtime: DbosRuntime) -> EffectIntent:
    """A format-3 Agent-Action line stopped at the effect a person must resolve."""

    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    revision = WorkflowRevision(ACTION_DOCUMENT)
    run_id = RunId("reconcile-run")
    run = start_published_v3_run(
        runtime.engine,
        runtime.settings,
        run_id,
        revision,
        runtime.agent_executor_registry,
    )
    _complete_the_action_predecessor(runtime, run)
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        run_id,
        revision.revision_hash,
        runtime.effect_adapter_binding,
    )
    with canonical_write_transaction(runtime.engine) as connection:
        connection.execute(
            effect_intents.update().values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=1,
            )
        )
        commit_reconciliation_required(
            connection,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            "action",
            intent.request.payload,
        )
    return intent


def _command(intent: EffectIntent, *, evidence: str = "inspected") -> ReconcileCommand:
    return ReconcileCommand(
        ReconcileCommandId("command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        evidence,
        OperatorFoundEffect(EffectId("effect"), EffectResult(b"result")),
    )


def test_reconciliation_created_and_existing_come_from_the_serialized_write(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    commander = DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    submitted = _command(intent)

    first = commander.submit_result(submitted)
    retry = commander.submit_result(submitted)

    assert isinstance(first, DurableReconciliationCreated)
    assert isinstance(retry, DurableReconciliationExisting)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(reconcile_commands)
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": reconcile_workflow_id_for(submitted.command_id)},
            )
            == 1
        )


def test_concurrent_same_command_http_reconciliation_creates_one_command_and_workflow(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    parallelism = 4
    barrier = Barrier(parallelism)
    path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(intent.binding.run_id)}"
        "/reconciliations"
    )
    body = {
        "command_id": "concurrent-http-command",
        "expected_intent_state_version": 1,
        "actor": "operator-concurrent",
        "evidence": "same exact concurrent evidence",
        "determination": {
            "type": "operator_found",
            "effect_id": "concurrent-effect",
            "result_base64": encode_canonical_base64(b"concurrent-result"),
        },
    }

    def reconcile(_: int) -> int:
        barrier.wait(timeout=5)
        return _client(runtime).post(path, json=body).status_code

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        statuses = list(pool.map(reconcile, range(parallelism)))

    assert statuses == [202] * parallelism
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(reconcile_commands)
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {
                    "workflow_id": reconcile_workflow_id_for(
                        ReconcileCommandId("concurrent-http-command")
                    )
                },
            )
            == 1
        )
        command_row = dict(
            connection.execute(
                sa.select(reconcile_commands).where(
                    reconcile_commands.c.command_id == "concurrent-http-command"
                )
            )
            .mappings()
            .one()
        )
        intent_row = dict(
            connection.execute(
                sa.select(effect_intents).where(
                    effect_intents.c.logical_key == intent.binding.logical_key.value
                )
            )
            .mappings()
            .one()
        )
    result_bytes = b"concurrent-result"
    assert command_row == {
        "command_id": body["command_id"],
        "logical_key": intent.binding.logical_key.value,
        "expected_intent_version": body["expected_intent_state_version"],
        "determination": "FOUND",
        "actor": body["actor"],
        "evidence": body["evidence"],
        "found_effect_id": body["determination"]["effect_id"],
        "found_result": result_bytes,
        "found_result_hash": hashlib.sha256(result_bytes).hexdigest(),
        "state": "PENDING",
    }
    assert intent_row == {
        "logical_key": RECONCILED_LOGICAL_KEY,
        "run_id": intent.binding.run_id.value,
        "canonical_request": RECONCILED_REQUEST,
        "request_hash": RECONCILED_REQUEST_HASH,
        "workflow_revision_hash": RECONCILED_REVISION_HASH,
        "adapter_revision": intent.binding.adapter_revision.value,
        "destination_identity": intent.binding.destination.value,
        "adapter_operational_identity": (
            intent.binding.adapter_operational_identity.value
        ),
        "operation_name": intent.binding.operation_name.value,
        "state": "RECONCILING",
        "state_version": 2,
        "reconciliation_owner_command_id": body["command_id"],
    }


def test_run_projection_over_response_limit_is_unrepresentable(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    commander = DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    oversized_evidence = "e" * 101
    assert isinstance(
        commander.submit_result(_command(intent, evidence=oversized_evidence)),
        DurableReconciliationCreated,
    )
    path = "/atelier/api/v1/runs/" + encode_public_run_reference(intent.binding.run_id)

    response = _client(runtime, api_limits(maximum_field_characters=100)).get(path)

    assert response.status_code == 500
    assert response.json()["type"].endswith(":durable-projection-unrepresentable")
    assert (
        response.json()["detail"] == "Inspect the durable projection before retrying."
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(reconcile_commands)
            )
            == 1
        )


def _client(
    runtime: DbosRuntime, configured_limits: ApiLimits | None = None
) -> TestClient:
    active_limits = api_limits() if configured_limits is None else configured_limits
    # The reader's bound comes from the same limits the app is configured with,
    # exactly as the host composes them; a test that let the two differ would be
    # asserting against a deployment that cannot exist.
    queries = DbosQueries(runtime.engine, durable_projection_limit(active_limits))
    return TestClient(
        create_app(
            source_commit="commit-under-test",
            source_tree="tree-under-test",
            ports=durable_ports(
                runtime.engine,
                runtime.settings,
                runtime.agent_executor_registry,
                queries=queries,
            ),
            limits=active_limits,
            event_poll_backoff=event_poll_backoff(),
        )
    )


def test_valid_revision_over_projection_limit_is_unrepresentable(
    runtime: DbosRuntime,
) -> None:
    # A V3 projection renders no per-field character bound; only its node count
    # is read-side-bounded, so the over-limit revision here is a valid document
    # whose node count exceeds a read configured tighter than the document's
    # own shape -- not, as a format-1 document could, an oversized field value.
    revision = WorkflowRevision(PAUSED_LINE_DOCUMENT)
    assert isinstance(
        DbosWorkflowRevisionPublisher(runtime.engine).publish(revision),
        DurableRevisionCreated,
    )
    with runtime.engine.connect() as connection:
        before = connection.scalar(
            sa.select(sa.func.count()).select_from(workflow_revisions)
        )

    response = _client(runtime, api_limits(maximum_workflow_nodes=1)).get(
        "/atelier/api/v1/workflow-revisions/" + revision.revision_hash.value
    )

    assert response.status_code == 500
    assert response.json()["type"].endswith(":durable-projection-unrepresentable")
    assert (
        response.json()["detail"] == "Inspect the durable projection before retrying."
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(workflow_revisions)
            )
            == before
        )


def test_http_publication_collision_changes_no_durable_state_or_workflow(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = WorkflowRevision(WAIT_SINK_DOCUMENT)
    stored_document = WAIT_SINK_DOCUMENT.replace(
        b"What is the answer?", b"What is a stored answer?"
    )
    with runtime.engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=requested.revision_hash.value,
                document=stored_document,
            )
        )

    @dataclass(frozen=True)
    class SimulatedCollisionRevision:
        document: bytes
        revision_hash: WorkflowRevisionHash

    monkeypatch.setattr(
        starter_module,
        "WorkflowRevision",
        lambda document: SimulatedCollisionRevision(document, requested.revision_hash),
    )
    before = _durable_snapshot(runtime)

    response = _client(runtime).post(
        "/atelier/api/v1/workflow-revisions",
        content=WAIT_SINK_DOCUMENT,
        headers={"content-type": "application/yaml"},
    )

    assert response.status_code == 409
    assert response.json()["type"].endswith(":revision-collision")
    assert _durable_snapshot(runtime) == before


def test_a_published_v3_revision_still_reaches_no_run(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    published = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=V3_DOCUMENT,
        headers={"content-type": "application/yaml"},
    )
    assert published.status_code == 201
    after_publication = _durable_snapshot(runtime)

    refused = client.post(
        "/atelier/api/v1/runs",
        json={
            "run_id": "v3-not-executable",
            "workflow_revision_hash": published.json()["workflow_revision_hash"],
        },
    )

    assert refused.status_code == 409
    assert refused.json()["type"].endswith(":workflow-format-not-executable")
    assert _durable_snapshot(runtime) == after_publication


def test_http_start_identity_conflict_changes_no_durable_state_or_workflow(
    runtime: DbosRuntime,
) -> None:
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    first_document = WAIT_SINK_DOCUMENT
    changed_document = _wait_sink_document("What is the answer now?")
    client = _client(runtime)
    for document in (first_document, changed_document):
        publication = client.post(
            "/atelier/api/v1/workflow-revisions",
            content=document,
            headers={"content-type": "application/yaml"},
        )
        assert publication.status_code == 201
    run_id = RunId("identity-conflict")
    created = client.post(
        "/atelier/api/v1/runs",
        json=_start_body(run_id, WorkflowRevision(first_document).revision_hash),
    )
    assert created.status_code == 201
    before = _durable_snapshot(runtime)

    conflict = client.post(
        "/atelier/api/v1/runs",
        json=_start_body(run_id, WorkflowRevision(changed_document).revision_hash),
    )

    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith(":run-identity-conflict")
    assert _durable_snapshot(runtime) == before


def test_http_publishes_lists_starts_and_reads_exact_durable_resources(
    runtime: DbosRuntime,
) -> None:
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    client = _client(runtime)

    published = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=WAIT_SINK_DOCUMENT,
        headers={"content-type": "application/yaml; charset=utf-8"},
    )
    retry = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=WAIT_SINK_DOCUMENT,
        headers={"content-type": "application/yaml"},
    )

    assert published.status_code == 201
    assert retry.status_code == 200
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    revision_hash = published.json()["workflow_revision_hash"]
    assert published.json() == retry.json()
    assert published.json()["document_base64"] == encode_canonical_base64(
        WAIT_SINK_DOCUMENT
    )
    assert [node["id"] for node in published.json()["graph"]["node_previews"]] == [
        "wait"
    ]
    assert (
        client.get(f"/atelier/api/v1/workflow-revisions/{revision_hash}").json()
        == published.json()
    )
    assert client.get("/atelier/api/v1/workflow-revisions").json() == {
        "items": [{"workflow_revision_hash": revision_hash}],
        "next_after_revision_hash": None,
    }

    run_ids = ("slash/run", "nul\0run", "Grüße-東京")
    for run_id in run_ids:
        body = _start_body(RunId(run_id), revision.revision_hash)
        created = client.post("/atelier/api/v1/runs", json=body)
        existing = client.post("/atelier/api/v1/runs", json=body)
        assert created.status_code == 201
        assert existing.status_code == 200
        assert created.json() == existing.json()
        reference = encode_public_run_reference(RunId(run_id))
        assert created.json()["public_run_reference"] == reference
        assert client.get(f"/atelier/api/v1/runs/{reference}").json() == created.json()

    listed = client.get("/atelier/api/v1/runs?limit=100").json()["items"]
    assert [item["run"]["run_id"] for item in listed] == sorted(
        run_ids, key=lambda value: value.encode("utf-8")
    )


def test_http_workflow_revision_pages_follow_every_exclusive_cursor(
    runtime: DbosRuntime,
) -> None:
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    client = _client(runtime)
    documents = tuple(
        _wait_sink_document(f"Which page is this, {index}?") for index in range(5)
    )
    expected = tuple(
        sorted(hashlib.sha256(document).hexdigest() for document in documents)
    )
    for document in documents:
        response = client.post(
            "/atelier/api/v1/workflow-revisions",
            content=document,
            headers={"content-type": "application/yaml"},
        )
        assert response.status_code == 201
        assert (
            response.json()["workflow_revision_hash"]
            == hashlib.sha256(document).hexdigest()
        )

    found: list[str] = []
    after: str | None = None
    for index, expected_hash in enumerate(expected):
        parameters = {"limit": "1"}
        if after is not None:
            parameters["after_revision_hash"] = after
        response = client.get("/atelier/api/v1/workflow-revisions", params=parameters)
        assert response.status_code == 200
        page = response.json()
        assert page["items"] == [{"workflow_revision_hash": expected_hash}]
        expected_next = expected_hash if index < len(expected) - 1 else None
        assert page["next_after_revision_hash"] == expected_next
        found.append(page["items"][0]["workflow_revision_hash"])
        after = page["next_after_revision_hash"]

    assert tuple(found) == expected
    assert len(found) == len(set(found))
    missing_boundary = next(
        candidate
        for candidate in ("0" * 64, "8" * 64, "f" * 64)
        if candidate not in expected
    )
    boundary_page = client.get(
        "/atelier/api/v1/workflow-revisions",
        params={"after_revision_hash": missing_boundary, "limit": "100"},
    )
    assert boundary_page.status_code == 200
    assert boundary_page.json() == {
        "items": [
            {"workflow_revision_hash": revision_hash}
            for revision_hash in expected
            if revision_hash > missing_boundary
        ],
        "next_after_revision_hash": None,
    }


def test_http_run_pages_follow_exact_utf8_order_and_every_exclusive_cursor(
    runtime: DbosRuntime,
) -> None:
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    client = _client(runtime)
    publication = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=WAIT_SINK_DOCUMENT,
        headers={"content-type": "application/yaml"},
    )
    assert publication.status_code == 201
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    run_ids = ("slash/run", "nul\0run", "Grüße-東京", "alpha", "zeta")
    expected = ("Grüße-東京", "alpha", "nul\0run", "slash/run", "zeta")
    assert expected == tuple(sorted(run_ids, key=lambda value: value.encode("utf-8")))
    for run_id in run_ids:
        response = client.post(
            "/atelier/api/v1/runs",
            json=_start_body(RunId(run_id), revision.revision_hash),
        )
        assert response.status_code == 201

    found: list[str] = []
    after: str | None = None
    for index, expected_run_id in enumerate(expected):
        parameters = {"limit": "1"}
        if after is not None:
            parameters["after"] = after
        response = client.get("/atelier/api/v1/runs", params=parameters)
        assert response.status_code == 200
        page = response.json()
        assert [item["run"]["run_id"] for item in page["items"]] == [expected_run_id]
        expected_next = (
            encode_public_run_reference(RunId(expected_run_id))
            if index < len(expected) - 1
            else None
        )
        assert page["next_after"] == expected_next
        found.append(page["items"][0]["run"]["run_id"])
        after = page["next_after"]

    assert tuple(found) == expected
    assert len(found) == len(set(found))
    missing_boundary = RunId("m")
    boundary_page = client.get(
        "/atelier/api/v1/runs",
        params={
            "after": encode_public_run_reference(missing_boundary),
            "limit": "100",
        },
    )
    assert boundary_page.status_code == 200
    assert [item["run"]["run_id"] for item in boundary_page.json()["items"]] == [
        "nul\0run",
        "slash/run",
        "zeta",
    ]
    assert boundary_page.json()["next_after"] is None


def test_http_wait_answer_retries_preserve_exact_bytes_and_status(
    runtime: DbosRuntime,
) -> None:
    run_id = RunId("answer/run")
    client = _client(runtime)
    waiting = _pause_for_an_answer(runtime, client, run_id)
    path = f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/answers"
    body = {
        "workflow_revision_hash": waiting.revision_hash.value,
        "node_id": "wait",
        "expected_node_execution_id": waiting.event.node_execution_id.value,
        "actor": "operator",
        "answer_base64": encode_canonical_base64(b"17"),
    }

    accepted = client.post(path, json=body)
    existing = client.post(path, json=body)
    before_conflict = _durable_snapshot(runtime)
    conflict = client.post(
        path,
        json={**body, "answer_base64": encode_canonical_base64(b"18")},
    )

    assert accepted.status_code == 202
    assert existing.status_code == 202
    assert accepted.json() == existing.json()
    assert accepted.json()["state"] == RunState.WAITING_INPUT.value
    assert accepted.json()["current_node_id"] == "wait"
    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith(":answer-state-conflict")
    assert _durable_snapshot(runtime) == before_conflict


def test_http_wait_answer_refuses_an_unsupported_actor_as_an_invalid_request(
    runtime: DbosRuntime,
) -> None:
    run_id = RunId("unsupported-answer-actor")
    revision, waiting = _paused_wait_line(runtime, run_id)
    before = _durable_snapshot(runtime)

    response = _client(runtime).post(
        f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/answers",
        json={
            "workflow_revision_hash": revision.revision_hash.value,
            "node_id": "wait",
            "expected_node_execution_id": waiting.event.node_execution_id.value,
            "actor": "stale-operator",
            "answer_base64": encode_canonical_base64(b"17"),
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert response.json()["invalid_fields"] == [
        {
            "path": "body/actor",
            "reason": "Input should be 'operator'",
        }
    ]
    assert _durable_snapshot(runtime) == before


def test_http_wait_answer_refuses_a_fabricated_execution_as_corrupt(
    runtime: DbosRuntime,
) -> None:
    run_id = RunId("fabricated-answer-execution")
    revision, _waiting = _paused_wait_line(runtime, run_id)
    before = _durable_snapshot(runtime)

    response = _client(runtime).post(
        f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/answers",
        json={
            "workflow_revision_hash": revision.revision_hash.value,
            "node_id": "wait",
            "expected_node_execution_id": "f" * 64,
            "actor": "operator",
            "answer_base64": encode_canonical_base64(b"17"),
        },
    )

    assert response.status_code == 500
    assert response.json()["type"].endswith(":durable-state-corrupt")
    assert _durable_snapshot(runtime) == before


def _applied_v3_wait_answer(
    runtime: DbosRuntime, run_id: RunId
) -> SubmitWaitAnswerRequest:
    client = _client(runtime)
    published_schema = client.post(
        "/atelier/api/v1/schema-revisions",
        content=ANY_JSON_SCHEMA.document,
        headers={"content-type": "application/json"},
    )
    assert published_schema.status_code == 201, published_schema.text
    published = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=WAIT_SINK_DOCUMENT,
        headers={"content-type": "application/yaml"},
    )
    assert published.status_code == 201, published.text
    revision_hash = WorkflowRevisionHash(published.json()["workflow_revision_hash"])
    started = client.post(
        "/atelier/api/v1/runs",
        json={
            "workflow_format_version": 3,
            "run_id": run_id.value,
            "workflow_revision_hash": revision_hash.value,
            "agent_bindings": [],
            "orders": [],
        },
    )
    assert started.status_code == 201, started.text
    with canonical_write_transaction(runtime.engine) as connection:
        waiting = commit_waiting_input(connection, run_id, revision_hash, "wait")
    request = SubmitWaitAnswerRequest(
        run_id,
        revision_hash,
        "wait",
        waiting.event.node_execution_id,
        WaitAnswerActor.OPERATOR,
        b"17",
    )
    accepted = client.post(_wait_answer_path(request), json=_wait_answer_body(request))
    assert accepted.status_code == 202, accepted.text
    with canonical_write_transaction(runtime.engine) as connection:
        pending = load_wait_answer(connection, run_id, revision_hash, "wait")
        commit_wait_answered(connection, pending.answer)
        applied = load_wait_answer(connection, run_id, revision_hash, "wait")
    assert applied.state is WaitAnswerState.APPLIED
    return request


def _wait_answer_path(request: SubmitWaitAnswerRequest) -> str:
    return f"/atelier/api/v1/runs/{encode_public_run_reference(request.run_id)}/answers"


def _wait_answer_body(request: SubmitWaitAnswerRequest) -> dict[str, str]:
    return {
        "workflow_revision_hash": request.revision_hash.value,
        "node_id": request.node_id,
        "expected_node_execution_id": request.expected_node_execution_id.value,
        "actor": request.actor.value,
        "answer_base64": encode_canonical_base64(request.answer_bytes),
    }


def _remove_recorded_wait_answer_actor(
    runtime: DbosRuntime, request: SubmitWaitAnswerRequest
) -> None:
    with runtime.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER wait_answers_payload_no_update")
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            wait_answers.update()
            .where(
                wait_answers.c.node_execution_id
                == request.expected_node_execution_id.value
            )
            .values(actor=None)
        )
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")


def test_http_same_answer_to_the_applied_current_execution_is_already_answered(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    request = _applied_v3_wait_answer(runtime, RunId("http-doubled-applied-answer"))
    before_second = _durable_snapshot(runtime)

    second = client.post(_wait_answer_path(request), json=_wait_answer_body(request))
    contradicting = client.post(
        _wait_answer_path(request),
        json={
            **_wait_answer_body(request),
            "answer_base64": encode_canonical_base64(b"a different answer"),
        },
    )

    assert second.status_code == 200
    assert second.json()["state"] == RunState.COMPLETED.value
    assert contradicting.status_code == 409
    assert contradicting.json()["type"].endswith(":answer-state-conflict")
    assert _durable_snapshot(runtime) == before_second

    with runtime.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER wait_answers_no_delete")
        connection.execute(
            wait_answers.delete().where(
                wait_answers.c.node_execution_id
                == request.expected_node_execution_id.value
            )
        )

    missing_answer = client.post(
        _wait_answer_path(request), json=_wait_answer_body(request)
    )

    assert missing_answer.status_code == 500
    assert missing_answer.json()["type"].endswith(":durable-state-corrupt")


def test_a_recorded_wait_answer_without_an_actor_is_durable_corruption(
    runtime: DbosRuntime,
) -> None:
    request = _applied_v3_wait_answer(runtime, RunId("recorded-answer-without-actor"))
    _remove_recorded_wait_answer_actor(runtime, request)

    with (
        runtime.engine.connect() as connection,
        pytest.raises(RunTransitionConflict, match="recorded wait answer has no actor"),
    ):
        load_wait_answer(
            connection,
            request.run_id,
            request.revision_hash,
            request.node_id,
        )


def test_a_v3_wait_answer_projection_refuses_a_recorded_row_without_an_actor(
    runtime: DbosRuntime,
) -> None:
    request = _applied_v3_wait_answer(
        runtime, RunId("project-recorded-answer-without-actor")
    )
    _remove_recorded_wait_answer_actor(runtime, request)
    with runtime.engine.connect() as connection:
        event_record = (
            connection.execute(
                sa.select(run_events).where(
                    run_events.c.node_execution_id
                    == request.expected_node_execution_id.value,
                    run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value,
                )
            )
            .mappings()
            .one()
        )

    with (
        runtime.engine.connect() as connection,
        pytest.raises(
            WaitAnswerProjectionCorrupt,
            match="wait answer event has no durable actor",
        ),
    ):
        DbosQueries._event_projection(
            connection,
            event_record,
            WorkflowFormatVersion.V3,
            durable_projection_limit(api_limits()),
        )


def test_an_applied_legacy_wait_answer_is_not_an_attributed_idempotent_hit(
    runtime: DbosRuntime,
) -> None:
    request = _applied_v3_wait_answer(runtime, RunId("applied-legacy-answer-retry"))
    with runtime.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER wait_answers_payload_no_update")
        connection.execute(
            wait_answers.update()
            .where(
                wait_answers.c.node_execution_id
                == request.expected_node_execution_id.value
            )
            .values(
                actor=None,
                actor_attribution_kind=(
                    WaitAnswerAttributionKind.LEGACY_UNATTRIBUTED.value
                ),
            )
        )

    response = _client(runtime).post(
        _wait_answer_path(request), json=_wait_answer_body(request)
    )

    assert response.status_code == 500
    assert response.json()["type"].endswith(":durable-state-corrupt")


@pytest.mark.parametrize(
    "corruption",
    (
        "missing-head",
        "contradictory-head",
        "missing-expected-actor",
        "waiting-head-at-nonwait-node",
        "answer-binding-disagrees",
        "applied-answer-at-waiting-head",
        "duplicate-answers",
    ),
)
def test_http_wait_answer_reports_a_corrupt_current_wait_as_definitive_500(
    runtime: DbosRuntime,
    corruption: str,
) -> None:
    run_id = RunId(f"corrupt-answer-{corruption}")
    revision, waiting = _paused_wait_line(runtime, run_id)
    execution_id = waiting.event.node_execution_id
    with runtime.engine.begin() as connection:
        if corruption == "missing-head":
            connection.exec_driver_sql("DROP TRIGGER run_events_no_delete")
            connection.execute(
                run_events.delete().where(
                    run_events.c.run_id == run_id.value,
                    run_events.c.event_sequence == waiting.last_event_sequence,
                )
            )
        elif corruption == "contradictory-head":
            connection.exec_driver_sql("DROP TRIGGER run_events_no_update")
            connection.execute(
                run_events.update()
                .where(
                    run_events.c.run_id == run_id.value,
                    run_events.c.event_sequence == waiting.last_event_sequence,
                )
                .values(node_execution_id="f" * 64)
            )
        elif corruption == "missing-expected-actor":
            connection.exec_driver_sql("DROP TRIGGER run_events_no_update")
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                run_events.update()
                .where(
                    run_events.c.run_id == run_id.value,
                    run_events.c.event_sequence == waiting.last_event_sequence,
                )
                .values(wait_answer_actor=None)
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        elif corruption == "waiting-head-at-nonwait-node":
            connection.execute(
                runs.update()
                .where(runs.c.run_id == run_id.value)
                .values(current_node_id="agent")
            )
        elif corruption == "answer-binding-disagrees":
            connection.execute(
                wait_answers.insert().values(
                    run_id=run_id.value,
                    revision_hash=revision.revision_hash.value,
                    node_id="agent",
                    node_execution_id=execution_id.value,
                    round_ordinal=waiting.current_round_ordinal,
                    actor=WaitAnswerActor.OPERATOR.value,
                    actor_attribution_kind="RECORDED",
                    answer_bytes=b"17",
                    answer_hash=Sha256Hash.of(b"17").value,
                    answer_workflow_id=answer_workflow_id_for(execution_id),
                    state="PENDING",
                    state_version=0,
                )
            )
        elif corruption == "applied-answer-at-waiting-head":
            connection.execute(
                wait_answers.insert().values(
                    run_id=run_id.value,
                    revision_hash=revision.revision_hash.value,
                    node_id="wait",
                    node_execution_id=execution_id.value,
                    round_ordinal=waiting.current_round_ordinal,
                    actor=WaitAnswerActor.OPERATOR.value,
                    actor_attribution_kind="RECORDED",
                    answer_bytes=b"17",
                    answer_hash=Sha256Hash.of(b"17").value,
                    answer_workflow_id=answer_workflow_id_for(execution_id),
                    state="APPLIED",
                    state_version=1,
                )
            )
        elif corruption == "duplicate-answers":
            for trigger in (
                "wait_answers_payload_no_update",
                "wait_answers_state_transition",
                "wait_answers_no_delete",
            ):
                connection.exec_driver_sql(f"DROP TRIGGER {trigger}")
            connection.exec_driver_sql(
                "CREATE TABLE duplicate_wait_answers AS "
                "SELECT * FROM wait_answers WHERE FALSE"
            )
            connection.exec_driver_sql("DROP TABLE wait_answers")
            connection.exec_driver_sql(
                "ALTER TABLE duplicate_wait_answers RENAME TO wait_answers"
            )
            duplicate = {
                "run_id": run_id.value,
                "revision_hash": revision.revision_hash.value,
                "node_id": "wait",
                "node_execution_id": execution_id.value,
                "round_ordinal": waiting.current_round_ordinal,
                "actor": WaitAnswerActor.OPERATOR.value,
                "actor_attribution_kind": "RECORDED",
                "answer_bytes": b"17",
                "answer_hash": Sha256Hash.of(b"17").value,
                "answer_workflow_id": answer_workflow_id_for(execution_id),
                "state": "PENDING",
                "state_version": 0,
            }
            connection.execute(wait_answers.insert(), (duplicate, duplicate))
        else:
            raise AssertionError(f"unknown corruption fixture: {corruption}")
    if corruption == "duplicate-answers":
        with (
            runtime.engine.connect() as connection,
            pytest.raises(WaitAnswerStateCorrupt) as raised,
        ):
            load_wait_answer(
                connection,
                run_id,
                revision.revision_hash,
                "wait",
            )
        assert not isinstance(raised.value, RunTransitionConflict)
    before = _durable_snapshot(runtime)
    response = _client(runtime).post(
        f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/answers",
        json={
            "workflow_revision_hash": revision.revision_hash.value,
            "node_id": "wait",
            "expected_node_execution_id": execution_id.value,
            "actor": "operator",
            "answer_base64": encode_canonical_base64(b"17"),
        },
    )

    assert response.status_code == 500
    assert response.json()["type"].endswith(":durable-state-corrupt")
    assert _durable_snapshot(runtime) == before


@pytest.mark.parametrize("found", [True, False])
def test_http_reconciliation_preserves_accountable_binding_without_adapter_identity(
    runtime: DbosRuntime, found: bool
) -> None:
    intent = _waiting_reconciliation(runtime)
    client = _client(runtime)
    path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(intent.binding.run_id)}"
        "/reconciliations"
    )
    determination = (
        {
            "type": "operator_found",
            "effect_id": "effect-http",
            "result_base64": encode_canonical_base64(b"result-http"),
        }
        if found
        else {"type": "operator_authoritative_absence"}
    )
    body = {
        "command_id": "http-command",
        "expected_intent_state_version": 1,
        "actor": "operator-http",
        "evidence": "inspected exact destination and request",
        "determination": determination,
    }

    accepted = client.post(path, json=body)
    existing = client.post(path, json=body)

    assert accepted.status_code == 202
    assert existing.status_code == 202
    assert accepted.json() == existing.json()
    assert accepted.json()["state"] == RunState.WAITING_RECONCILIATION.value
    with runtime.engine.connect() as connection:
        command = dict(
            connection.execute(
                sa.select(reconcile_commands).where(
                    reconcile_commands.c.command_id == body["command_id"]
                )
            )
            .mappings()
            .one()
        )
    assert (command["actor"], command["evidence"], command["state"]) == (
        body["actor"],
        body["evidence"],
        "PENDING",
    )
    assert (command["found_effect_id"], command["found_result"]) == (
        ("effect-http", b"result-http") if found else (None, None)
    )
    encoded = str(accepted.json())
    assert "adapter" not in encoded
    assert "workflow_id" not in encoded
    assert "sqlite" not in encoded


def test_http_reconciliation_preserves_empty_result_bytes_on_exact_retry(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    client = _client(runtime)
    path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(intent.binding.run_id)}"
        "/reconciliations"
    )
    body = {
        "command_id": "empty-result-command",
        "expected_intent_state_version": 1,
        "actor": "operator-empty",
        "evidence": "destination returned exact empty bytes",
        "determination": {
            "type": "operator_found",
            "effect_id": "empty-effect",
            "result_base64": "",
        },
    }

    accepted = client.post(path, json=body)
    existing = client.post(path, json=body)

    assert accepted.status_code == existing.status_code == 202
    assert accepted.json() == existing.json()
    with runtime.engine.connect() as connection:
        command = dict(
            connection.execute(
                sa.select(reconcile_commands).where(
                    reconcile_commands.c.command_id == "empty-result-command"
                )
            )
            .mappings()
            .one()
        )
    assert command["found_result"] == b""
    assert command["found_result_hash"] == hashlib.sha256(b"").hexdigest()


def test_http_stale_reconciliation_persists_rejection_then_exact_retry_reports_it(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    client = _client(runtime)
    path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(intent.binding.run_id)}"
        "/reconciliations"
    )
    body = {
        "command_id": "stale-command",
        "expected_intent_state_version": 0,
        "actor": "operator-stale",
        "evidence": "stale observation retained for accountability",
        "determination": {"type": "operator_authoritative_absence"},
    }
    before = _durable_snapshot(runtime)

    stale = client.post(path, json=body)
    after_stale = _durable_snapshot(runtime)
    retry = client.post(path, json=body)

    assert stale.status_code == 409
    assert stale.json()["type"].endswith(":reconciliation-stale")
    assert after_stale != before
    assert retry.status_code == 409
    assert retry.json()["type"].endswith(":reconciliation-rejected")
    assert _durable_snapshot(runtime) == after_stale
    with runtime.engine.connect() as connection:
        command = dict(
            connection.execute(
                sa.select(reconcile_commands).where(
                    reconcile_commands.c.command_id == "stale-command"
                )
            )
            .mappings()
            .one()
        )
    assert command == {
        "command_id": "stale-command",
        "logical_key": intent.binding.logical_key.value,
        "expected_intent_version": 0,
        "determination": "AUTHORITATIVE_NOT_FOUND",
        "actor": "operator-stale",
        "evidence": "stale observation retained for accountability",
        "found_effect_id": None,
        "found_result": None,
        "found_result_hash": None,
        "state": "REJECTED_CONFLICT",
    }


def test_http_reconciliation_exact_applied_retry_survives_run_advancement(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    client = _client(runtime)
    path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(intent.binding.run_id)}"
        "/reconciliations"
    )
    determination = OperatorFoundEffect(
        EffectId("effect-applied"), EffectResult(b"result-applied")
    )
    command = ReconcileCommand(
        ReconcileCommandId("applied-command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator-applied"),
        "inspected exact applied request",
        determination,
    )
    body = {
        "command_id": command.command_id.value,
        "expected_intent_state_version": 1,
        "actor": command.actor.value,
        "evidence": command.evidence,
        "determination": {
            "type": "operator_found",
            "effect_id": determination.effect_id.value,
            "result_base64": encode_canonical_base64(determination.result.payload),
        },
    }

    accepted = client.post(path, json=body)
    assert accepted.status_code == 202
    result_hash = RECONCILED_APPLIED_RESULT_HASH
    logical_key = RECONCILED_LOGICAL_KEY
    expected_intent = {
        "logical_key": logical_key,
        "run_id": intent.binding.run_id.value,
        "canonical_request": RECONCILED_REQUEST,
        "request_hash": RECONCILED_REQUEST_HASH,
        "workflow_revision_hash": RECONCILED_REVISION_HASH,
        "adapter_revision": intent.binding.adapter_revision.value,
        "destination_identity": intent.binding.destination.value,
        "adapter_operational_identity": (
            intent.binding.adapter_operational_identity.value
        ),
        "operation_name": intent.binding.operation_name.value,
        "state": "RECONCILING",
        "state_version": 2,
        "reconciliation_owner_command_id": "applied-command",
    }
    expected_command = {
        "command_id": "applied-command",
        "logical_key": logical_key,
        "expected_intent_version": 1,
        "determination": "FOUND",
        "actor": "operator-applied",
        "evidence": "inspected exact applied request",
        "found_effect_id": "effect-applied",
        "found_result": b"result-applied",
        "found_result_hash": result_hash,
        "state": "PENDING",
    }
    with runtime.engine.connect() as connection:
        intent_row = (
            connection.execute(
                sa.select(effect_intents).where(
                    effect_intents.c.logical_key == logical_key
                )
            )
            .mappings()
            .one()
        )
        command_row = (
            connection.execute(
                sa.select(reconcile_commands).where(
                    reconcile_commands.c.command_id == "applied-command"
                )
            )
            .mappings()
            .one()
        )
    assert dict(intent_row) == expected_intent
    assert dict(command_row) == expected_command

    with Session(runtime.engine) as session, session.begin():
        commit_resolution(
            session,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
            encode_found(
                PerformedEffect(determination.effect_id, determination.result),
                ConfirmationSource.OPERATOR_FOUND,
                command.command_id,
            ),
            command.command_id,
        )

    with runtime.engine.connect() as connection:
        receipt_row = (
            connection.execute(
                sa.select(effect_receipts).where(
                    effect_receipts.c.logical_key == logical_key
                )
            )
            .mappings()
            .one()
        )
        applied_command_row = (
            connection.execute(
                sa.select(reconcile_commands).where(
                    reconcile_commands.c.command_id == "applied-command"
                )
            )
            .mappings()
            .one()
        )
        confirmed_intent_row = (
            connection.execute(
                sa.select(effect_intents).where(
                    effect_intents.c.logical_key == logical_key
                )
            )
            .mappings()
            .one()
        )
    assert dict(receipt_row) == {
        "logical_key": logical_key,
        "run_id": intent.binding.run_id.value,
        "canonical_request": RECONCILED_REQUEST,
        "request_hash": RECONCILED_REQUEST_HASH,
        "workflow_revision_hash": RECONCILED_REVISION_HASH,
        "adapter_revision": intent.binding.adapter_revision.value,
        "destination_identity": intent.binding.destination.value,
        "adapter_operational_identity": (
            intent.binding.adapter_operational_identity.value
        ),
        "operation_name": intent.binding.operation_name.value,
        "effect_id": "effect-applied",
        "result": b"result-applied",
        "result_hash": result_hash,
        "confirmation_source": "OPERATOR_FOUND",
        "reconcile_command_id": "applied-command",
        "fork_source_logical_key": None,
        "fork_source_run_id": None,
        "fork_source_workflow_revision_hash": None,
        "fork_source_result_hash": None,
    }
    assert dict(applied_command_row) == {**expected_command, "state": "APPLIED"}
    assert dict(confirmed_intent_row) == {
        **expected_intent,
        "state": "CONFIRMED",
        "state_version": 3,
        "reconciliation_owner_command_id": None,
    }

    retry = client.post(path, json=body)

    assert retry.status_code == 200
    assert retry.json()["state"] == RunState.STARTED.value


def test_reconciliation_retry_conflicts_are_typed_without_mutation(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    commander = DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    submitted = _command(intent)
    assert isinstance(commander.submit_result(submitted), DurableReconciliationCreated)
    before_conflicts = _durable_snapshot(runtime)

    changed_command = commander.submit_result(_command(intent, evidence="changed"))
    assert isinstance(changed_command, DurableReconciliationCommandConflict)
    assert _durable_snapshot(runtime) == before_conflicts
    changed_determination = commander.submit_result(
        ReconcileCommand(
            submitted.command_id,
            submitted.intent_reference,
            submitted.expected_intent_state_version,
            submitted.actor,
            submitted.evidence,
            OperatorFoundEffect(EffectId("different"), EffectResult(b"different")),
        )
    )

    assert isinstance(changed_determination, DurableReconciliationDeterminationConflict)
    assert _durable_snapshot(runtime) == before_conflicts
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(reconcile_commands)
            )
            == 1
        )


def _assert_parser_has_no_serialized_write_lock(
    runtime: DbosRuntime, parser: object, document: bytes
):
    with runtime.engine.connect() as connection:
        previous_timeout = int(
            connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        )
        try:
            connection.exec_driver_sql("PRAGMA busy_timeout=1")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.rollback()
        finally:
            connection.exec_driver_sql(f"PRAGMA busy_timeout={previous_timeout}")
    assert callable(parser)
    return parser(document)


def test_start_parses_workflow_before_begin_immediate(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    publisher = DbosWorkflowRevisionPublisher(runtime.engine)
    assert isinstance(publisher.publish(revision), DurableRevisionCreated)
    original_parser = starter_module.parse_workflow_document
    monkeypatch.setattr(
        starter_module,
        "parse_workflow_document",
        lambda document: _assert_parser_has_no_serialized_write_lock(
            runtime, original_parser, document
        ),
    )

    result = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        NO_AGENT_EXECUTORS,
    ).start_published(
        StartPublishedRunRequest(
            RunId("parse-before-start-lock"), revision.revision_hash
        )
    )

    assert isinstance(result, DurableRunCreated)


def test_wait_answer_parses_workflow_before_begin_immediate(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = RunId("parse-before-answer-lock")
    revision, _waiting = _paused_wait_line(runtime, run_id)
    original_parser = run_transitions_module.parse_executable_workflow_document
    monkeypatch.setattr(
        run_transitions_module,
        "parse_executable_workflow_document",
        lambda document: _assert_parser_has_no_serialized_write_lock(
            runtime, original_parser, document
        ),
    )

    result = DbosWaitAnswerer(
        runtime.engine, runtime.settings.application_version
    ).submit_result(
        SubmitWaitAnswerRequest(
            run_id,
            revision.revision_hash,
            "wait",
            NodeExecutionId.for_node(run_id, revision.revision_hash, "wait"),
            WaitAnswerActor.OPERATOR,
            b"17",
        )
    )

    assert isinstance(result, DurableAnswerCreated)


def test_start_rechecks_revision_bytes_after_outside_parse_without_mutation(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_referenced_revisions(runtime, ANY_JSON_SCHEMA)
    revision = WorkflowRevision(WAIT_SINK_DOCUMENT)
    assert isinstance(
        DbosWorkflowRevisionPublisher(runtime.engine).publish(revision),
        DurableRevisionCreated,
    )
    changed_document = WAIT_SINK_DOCUMENT.replace(
        b"What is the answer?", b"What is a different answer?"
    )
    original_parser = starter_module.parse_workflow_document

    def drift_revision(document: bytes):
        graph = original_parser(document)
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER workflow_revisions_no_update")
            connection.execute(
                workflow_revisions.update()
                .where(
                    workflow_revisions.c.revision_hash == revision.revision_hash.value
                )
                .values(document=changed_document)
            )
        return graph

    monkeypatch.setattr(starter_module, "parse_workflow_document", drift_revision)
    with runtime.engine.connect() as connection:
        before_runs = int(
            connection.scalar(sa.select(sa.func.count()).select_from(runs)) or 0
        )
        before_workflows = int(
            connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_status")) or 0
        )

    result = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        NO_AGENT_EXECUTORS,
    ).start_published(
        StartPublishedRunRequest(RunId("revision-drift-start"), revision.revision_hash)
    )

    assert isinstance(result, DurableStateCorrupt)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(runs))
            == before_runs
        )
        assert (
            connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_status"))
            == before_workflows
        )


def test_wait_answer_rechecks_revision_bytes_after_outside_parse_without_mutation(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = RunId("revision-drift-answer")
    revision, _waiting = _paused_wait_line(runtime, run_id)
    changed_document = PAUSED_LINE_DOCUMENT.replace(
        b"Did the agent's work hold?", b"Did the agent's work really hold?"
    )
    original_parser = run_transitions_module.parse_executable_workflow_document

    def drift_revision(document: bytes):
        graph = original_parser(document)
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER workflow_revisions_no_update")
            connection.execute(
                workflow_revisions.update()
                .where(
                    workflow_revisions.c.revision_hash == revision.revision_hash.value
                )
                .values(document=changed_document)
            )
        return graph

    monkeypatch.setattr(
        run_transitions_module, "parse_executable_workflow_document", drift_revision
    )
    with runtime.engine.connect() as connection:
        before_answers = int(
            connection.scalar(sa.select(sa.func.count()).select_from(wait_answers)) or 0
        )
        before_workflows = int(
            connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_status")) or 0
        )

    result = DbosWaitAnswerer(
        runtime.engine, runtime.settings.application_version
    ).submit_result(
        SubmitWaitAnswerRequest(
            run_id,
            revision.revision_hash,
            "wait",
            NodeExecutionId.for_node(run_id, revision.revision_hash, "wait"),
            WaitAnswerActor.OPERATOR,
            b"17",
        )
    )

    assert isinstance(result, DurableStateCorrupt)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(wait_answers))
            == before_answers
        )
        assert (
            connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_status"))
            == before_workflows
        )
