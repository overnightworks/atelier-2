"""A V3 line stops for a person, survives as a stopped run, and is carried on.

The agent half of #194 proved a line that runs to its end without a hand
reaching in. This is the half where a hand is exactly the point: the runtime
reaches a Wait node, writes WAITING_INPUT durably and stops -- no queue work
pending, nothing polling -- and the run stays there until an answer arrives
through the surface an operator actually uses. The answer then carries it to the
run's terminal hash.

What is driven here is the answer use case, one layer under the route that calls
it; #219 owns the HTTP resource and proves the route itself. What is asserted is
what an operator would see: the pause, the two named refusals around it, and the
line finishing on a hash that recomputes from the events the run wrote.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from dbos import DBOS

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.names import ANSWER_WORKFLOW_NAME
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    commit_wait_answered,
    load_wait_answer,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, create_canonical_engine
from atelier2.adapters.dbos.schema import run_events, runs, wait_answers
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
from atelier2.api.projection.runs import run_resource
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerExistingApplied,
    AnswerExistingPending,
    AnswerStateConflict,
    UnanswerableWait,
    answer_wait_result,
)
from atelier2.application.cancel_run import (
    CancelEndedRun,
    CancelNotCancellable,
    CancelRunResult,
    cancel_run_result,
)
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
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
    WaitAnswerState,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_cancellations import RunCancelCommandId
from atelier2.contracts.run_projections import NodeState, RunCancellationRefusal
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableAnswerNotAdmitted,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound, RunFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.run_waiting import wait_for_workflow_completion
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

APPROVAL_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA, b'{"type": "string", "minLength": 1}'
)
"""The contract the wait node pins, narrow enough to refuse a wrong answer.

The pause is the subject here, so the schema is the smallest one that can say no
to something. A person's answer is authored, not produced
(`schemas_v3.read_authored_instance_document`): the bytes typed ARE the string,
so a bare `41` or `{"verdict": "approved"}` is now an admitted answer's own
text rather than a JSON value of the wrong shape. `minLength` keeps one thing
this schema can still refuse -- silence -- because a schema admitting every
byte string would make the refusal below unprovable.
"""

WAIT_IN_THE_MIDDLE = (
    b"""format_version: 3
name: A person approves between two agents
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
"""
    + declared_output(APPROVAL_SCHEMA, "approval")
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the person approved.
    depends_on: [approve]
"""
    + declared_output()
)

WAIT_AS_THE_SINK = (
    b"""format_version: 3
name: A person approves last
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
"""
    + declared_output(APPROVAL_SCHEMA, "approval")
)

WAIT_WITH_AN_UNWRITTEN_INPUT = (
    b"""format_version: 3
name: A person waits for a named predecessor value
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Produce the candidate a person will review.
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve the candidate bound below.
    depends_on: [implement]
    inputs:
      - name: candidate
        from:
          node: implement
          output: result
"""
    + declared_output(APPROVAL_SCHEMA, "approval")
)

RUN = RunId("v3/a-person-approves")
PROVIDER_OUTPUT = b'"the exact provider bytes"'
ANSWER = b'"approved"'
WAIT_NODE = "approve"


def recording_provider() -> RecordingAgentExecutorFactoryV2:
    """The agent executor that succeeds, so the line reaches the pause."""
    return RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )


def wait_runtime_over(
    root: Path, recording: RecordingAgentExecutorFactoryV2
) -> DbosRuntime:
    """A runtime over the durable state in this directory, as a restart builds one."""
    return DbosRuntime(
        canonical_runtime_settings(root, "v3-wait-test", agent_scratch_root(root)),
        canonical_loopback_effects(root),
        (recording,),
    )


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    recording = recording_provider()
    started = wait_runtime_over(tmp_path, recording)
    started.initialize_storage()
    try:
        yield started, recording
    finally:
        started.close()


def publish_and_start(runtime: DbosRuntime, document: bytes) -> WorkflowRevision:
    """Publish everything the document pins and start its durable run."""
    catalog_store = DbosCatalogStore(runtime.engine)
    for schema in (ANY_JSON_SCHEMA, APPROVAL_SCHEMA):
        assert isinstance(
            catalog_store.publish_revision(schema),
            (PublishedRevisionCreated, PublishedRevisionExisting),
        )
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            RUN,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
            ),
        )
    )
    assert isinstance(started, DurableRunCreated), started
    return workflow


def start_and_launch(runtime: DbosRuntime, document: bytes) -> WorkflowRevision:
    """Publish and start the run, then let its durable driver move."""
    workflow = publish_and_start(runtime, document)
    runtime.launch()
    return workflow


def wait_for_wait_node(runtime: DbosRuntime) -> None:
    with runtime.engine.connect() as connection:
        revision_hash = connection.scalar(
            sa.select(runs.c.revision_hash).where(runs.c.run_id == RUN.value)
        )
    assert revision_hash is not None
    assert (
        wait_for_workflow_completion(
            node_workflow_id_for(
                NodeExecutionId.for_node(
                    RUN, WorkflowRevisionHash(str(revision_hash)), WAIT_NODE
                )
            ),
            "the wait node to write WAITING_INPUT",
        )
        == RunState.WAITING_INPUT.value
    )


def wait_for_answer_completion(runtime: DbosRuntime) -> None:
    with runtime.engine.connect() as connection:
        answer_workflow_id = connection.scalar(
            sa.select(wait_answers.c.answer_workflow_id).where(
                wait_answers.c.run_id == RUN.value,
                wait_answers.c.node_id == WAIT_NODE,
            )
        )
    assert answer_workflow_id is not None
    answer_result = wait_for_workflow_completion(
        str(answer_workflow_id), "the wait answer to finish"
    )
    if answer_result == RunState.COMPLETED.value:
        return
    assert answer_result == RunState.STARTED.value
    with runtime.engine.connect() as connection:
        head = connection.execute(
            sa.select(runs.c.revision_hash, runs.c.current_node_id).where(
                runs.c.run_id == RUN.value
            )
        ).one()
    assert (
        wait_for_workflow_completion(
            node_workflow_id_for(
                NodeExecutionId.for_node(
                    RUN,
                    WorkflowRevisionHash(str(head.revision_hash)),
                    str(head.current_node_id),
                )
            ),
            "the wait answer's successor node to complete",
        )
        == RunState.COMPLETED.value
    )


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    if state is RunState.WAITING_INPUT:
        wait_for_wait_node(runtime)
        return
    assert state is RunState.COMPLETED
    wait_for_answer_completion(runtime)


def durable_events(runtime: DbosRuntime) -> list[tuple[int, str, str, bytes]]:
    with runtime.engine.connect() as connection:
        return [
            (
                int(str(record["event_sequence"])),
                str(record["node_id"]),
                str(record["event_kind"]),
                bytes(record["payload"]),
            )
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]


def answer(runtime: DbosRuntime, workflow: WorkflowRevision, value: bytes) -> object:
    """Answer the waiting node exactly as the API route does, one layer under it."""
    return answer_wait_result(
        RUN,
        workflow.revision_hash,
        WAIT_NODE,
        NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE),
        WaitAnswerActor.OPERATOR,
        value,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )


def cancel(
    engine: sa.Engine,
    application_version: str,
    workflow: WorkflowRevision,
    idempotency_key: str,
    *,
    node_id: str = WAIT_NODE,
) -> CancelRunResult:
    """Cancel the run exactly as the route does, one layer under it.

    `node_id` is what the operator's confirmation fenced on, so a test can hand
    over the execution of some other node and watch the fence refuse it.
    """
    return cancel_run_result(
        RUN,
        idempotency_key,
        NodeExecutionId.for_node(RUN, workflow.revision_hash, node_id),
        DbosAgentAttemptStore(engine, application_version),
    )


def what_the_answer_workflow_recorded(
    runtime: DbosRuntime,
) -> tuple[str, tuple[str, ...]]:
    """The answer workflow's own durable record: its result, and what it started.

    Read through DBOS rather than off the run, because recovery replays exactly
    this record. The started ids are the child workflows the answer drove.
    """
    with runtime.engine.connect() as connection:
        workflow_id = str(
            connection.scalar(
                sa.select(wait_answers.c.answer_workflow_id).where(
                    wait_answers.c.run_id == RUN.value,
                    wait_answers.c.node_id == WAIT_NODE,
                )
            )
        )
    status = DBOS.get_workflow_status(workflow_id)
    assert status is not None
    assert status.name == ANSWER_WORKFLOW_NAME
    return (
        str(DBOS.retrieve_workflow(workflow_id).get_result()),
        tuple(
            str(step["child_workflow_id"])
            for step in DBOS.list_workflow_steps(workflow_id)
            if step["child_workflow_id"] is not None
        ),
    )


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_v3_line_holds_at_its_wait_node_until_a_person_answers_it(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The pause is durable, visible, and nothing moves the run while it holds.

    Held rather than merely reached: after the run reports WAITING_INPUT it is
    read again, and the agent executor is asked how many nodes it ran. A pause
    that some queue quietly drove past would show the second agent's attempt
    here, and the run would have moved on without the person it is waiting for.
    """
    started, recording = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)

    wait_for_state(started, RunState.WAITING_INPUT)

    with started.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    assert (str(run["state"]), str(run["current_node_id"])) == (
        RunState.WAITING_INPUT.value,
        WAIT_NODE,
    )
    assert run["terminal_hash"] is None
    assert durable_events(started) == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, WAIT_NODE, RunEventKind.WAITING_INPUT.value, b""),
    ]
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == ["implement"]
    assert workflow.revision_hash.value == str(run["revision_hash"])


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_waiting_v3_run_reads_back_as_waiting_with_the_node_that_owes_a_move(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A reader is told the run is waiting and which node the person is at.

    This is the half an operator sees. The run resource says WAITING_INPUT rather
    than a state a V3 run supposedly cannot reach, and the rail marks the wait
    node as the one owing a move while the node before it reads as done.
    """
    started, _ = runtime
    start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    read = durable_queries(started.engine).get_run(RUN)

    assert isinstance(read, RunFound), read
    resource = run_resource(read.projection).model_dump(mode="json")
    assert resource["workflow_format_version"] == 3
    assert resource["state"] == RunState.WAITING_INPUT.value
    assert resource["current_node_id"] == WAIT_NODE
    assert resource["terminal_hash"] is None
    assert [(entry["node_id"], entry["state"]) for entry in resource["node_rail"]] == [
        ("implement", NodeState.SUCCEEDED.value),
        (WAIT_NODE, NodeState.NEEDS_YOU.value),
        ("review", NodeState.QUEUED.value),
    ]


@pytest.mark.proves("a-waiting-v3-run-is-answerable-on-its-run-page")
def test_a_no_input_wait_reads_answers_and_rereads_its_authored_prompt(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A no-input Wait keeps its published question and empty pause payload."""
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    found = durable_queries(started.engine).get_node_detail(RUN, WAIT_NODE)

    assert isinstance(found, NodeDetailFound), found
    assert found.detail.job == b"Approve this candidate, or name the blocking defect."
    assert found.detail.job_hash == Sha256Hash.of(found.detail.job).value
    with started.engine.connect() as connection:
        pause = (
            connection.execute(
                sa.select(run_events).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.node_id == WAIT_NODE,
                    run_events.c.event_kind == RunEventKind.WAITING_INPUT.value,
                )
            )
            .mappings()
            .one()
        )
    assert bytes(pause["payload"]) == b""
    assert str(pause["payload_hash"]) == Sha256Hash.of(b"").value

    accepted = answer(started, workflow, ANSWER)
    assert isinstance(accepted, AnswerAcceptedPending), accepted
    wait_for_state(started, RunState.COMPLETED)
    reread = durable_queries(started.engine).get_node_detail(RUN, WAIT_NODE)
    assert isinstance(reread, NodeDetailFound), reread
    assert reread.detail.state is NodeState.SUCCEEDED
    assert reread.detail.job == found.detail.job
    assert reread.detail.job_hash == found.detail.job_hash
    assert reread.detail.answer is not None
    assert reread.detail.answer.value == ANSWER


def test_a_nonlive_bound_wait_names_the_predecessor_that_never_wrote(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """Missing input is soft while queued and named if a corrupt run pauses."""
    started, _ = runtime
    publish_and_start(started, WAIT_WITH_AN_UNWRITTEN_INPUT)

    queued = durable_queries(started.engine).get_node_detail(RUN, WAIT_NODE)
    assert isinstance(queued, NodeDetailFound), queued
    assert queued.detail.state is NodeState.QUEUED
    assert queued.detail.job is None
    assert queued.detail.refusal is None

    with started.engine.begin() as connection:
        changed = connection.execute(
            runs.update()
            .where(runs.c.run_id == RUN.value)
            .values(
                current_node_id=WAIT_NODE,
                state=RunState.WAITING_INPUT.value,
                state_version=1,
            )
        )
    assert changed.rowcount == 1
    with started.engine.connect() as connection:
        pause_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(run_events)
            .where(
                run_events.c.run_id == RUN.value,
                run_events.c.event_kind == RunEventKind.WAITING_INPUT.value,
            )
        )
    assert pause_count == 0

    stopped = durable_queries(started.engine).get_node_detail(RUN, WAIT_NODE)
    assert isinstance(stopped, NodeDetailFound), stopped
    assert stopped.detail.state is NodeState.NEEDS_YOU
    assert stopped.detail.job is None
    assert stopped.detail.refusal is not None
    assert "node 'implement'" in stopped.detail.refusal
    assert "has written no output" in stopped.detail.refusal


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answer_carries_a_waiting_v3_line_on_to_its_terminal_hash(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The person's answer is what restarts the line, and the line then ends.

    The answer's bytes are kept as the event's payload, so the terminal hash the
    run settles on folds in what the person actually said: a different answer is
    a different chain, which is the whole reason the pause is part of the record.
    """
    started, recording = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    accepted = answer(started, workflow, ANSWER)

    assert isinstance(accepted, AnswerAcceptedPending), accepted
    wait_for_state(started, RunState.COMPLETED)
    assert durable_events(started) == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, WAIT_NODE, RunEventKind.WAITING_INPUT.value, b""),
        (3, WAIT_NODE, RunEventKind.WAIT_ANSWERED.value, ANSWER),
        (4, "review", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
    ]
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == [
        "implement",
        "review",
    ]
    with started.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
        answer_state = connection.scalar(
            sa.select(wait_answers.c.state).where(
                wait_answers.c.run_id == RUN.value,
                wait_answers.c.node_id == WAIT_NODE,
            )
        )
    assert str(run["current_node_id"]) == "review"
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )
    assert str(answer_state) == WaitAnswerState.APPLIED.value

    # #511: the answer this test just drove through is readable on its own node,
    # not only folded into the run's terminal hash.
    found = durable_queries(started.engine).get_node_detail(RUN, WAIT_NODE)
    assert isinstance(found, NodeDetailFound), found
    assert found.detail.answer is not None
    assert found.detail.answer.value == ANSWER
    assert found.detail.answer.value_hash == Sha256Hash.of(ANSWER)


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answered_wait_standing_last_completes_its_own_run(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A pause at the end of the line is the node that ends the run.

    The completion rule is the one every other node is asked, so a Wait node that
    nothing depends on carries the run to COMPLETED itself rather than handing on
    to a successor its author never declared.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    assert durable_events(started) == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, WAIT_NODE, RunEventKind.WAITING_INPUT.value, b""),
        (3, WAIT_NODE, RunEventKind.WAIT_ANSWERED.value, ANSWER),
    ]
    with started.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
    assert str(run["current_node_id"]) == WAIT_NODE
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_the_answer_that_ends_a_run_reports_completion_and_starts_nothing_after_it(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The answer workflow of a sink records COMPLETED and drives no node after it.

    This is the record recovery replays, so it is what has to be honest rather
    than merely harmless. Reporting STARTED for a finished run would be the
    workflow misdescribing itself, and starting a node after a terminal
    transition is only free while DBOS happens to deduplicate the sink's own
    workflow id -- correctness after the run has ended must not rest on that.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    assert what_the_answer_workflow_recorded(started) == (RunState.COMPLETED.value, ())


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_the_answer_that_carries_a_run_on_reports_it_started_the_next_node(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """Where a successor is owed, the same record says STARTED and names its child.

    The counterpart of the sink case: the two branches of the answer workflow are
    told apart by what each one records, so neither reading can be reached by the
    other run's shape.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    result, started_children = what_the_answer_workflow_recorded(started)
    assert result == RunState.STARTED.value
    assert started_children == (
        node_workflow_id_for(
            NodeExecutionId.for_node(RUN, workflow.revision_hash, "review")
        ),
    )


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_stored_answer_names_the_exact_execution_of_the_node_it_answers(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The row is keyed by execution and round, not by the node the person saw.

    A node a declared loop turns pauses once per round, so an answer that named
    only run and node would let a message typed for one round be applied to a
    later one. What is asserted is the key the store really holds.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    with started.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(wait_answers).where(wait_answers.c.run_id == RUN.value)
            )
            .mappings()
            .one()
        )
        paused_event = (
            connection.execute(
                sa.select(run_events).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value,
                )
            )
            .mappings()
            .one()
        )
    execution = NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE)
    assert str(stored["node_execution_id"]) == execution.value
    assert int(stored["round_ordinal"]) == FIRST_ROUND_ORDINAL
    assert str(paused_event["node_execution_id"]) == execution.value
    assert int(paused_event["round_ordinal"]) == FIRST_ROUND_ORDINAL


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_already_applied_answer_replays_without_a_second_event(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """Committing an answer that is already applied answers the same and writes nothing.

    This is the branch a replay takes, and only this: a process that dies
    between the commit and the record of it comes back to an answer already
    APPLIED, and the second call has to be the first one's answer rather than a
    second transition on a run that has already moved. What proves it is the
    event log rather than the return value alone -- the run's events are read
    before and after, and they are the same list.

    It says nothing about how many arguments the recovered workflow carried;
    that is the migration suite's driven proof.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)
    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)
    wait_for_state(started, RunState.COMPLETED)
    settled = durable_events(started)

    # The replay commits, because a transaction that rolls back on the way out
    # would hide the very write this test is looking for.
    with started.engine.begin() as connection:
        applied = load_wait_answer(connection, RUN, workflow.revision_hash, WAIT_NODE)
        replayed = commit_wait_answered(connection, applied.answer)

    assert applied.state is WaitAnswerState.APPLIED
    assert replayed.event.event_kind is RunEventKind.WAIT_ANSWERED
    assert replayed.state is RunState.COMPLETED
    assert replayed.current_round_ordinal == FIRST_ROUND_ORDINAL
    assert durable_events(started) == settled


@pytest.mark.parametrize(
    "rejected",
    [b"", b"\xff"],
    ids=["nothing at all", "bytes that are no text"],
)
@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answer_the_waits_own_schema_refuses_leaves_the_run_waiting(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
    rejected: bytes,
) -> None:
    """The schema the author pinned is what judges the answer, and it may say no.

    Refused rather than kept: nothing durable is written, the run is still owed
    an answer, and the person can send another. The judge is the same schema
    owner that reads every value this run produces, so an answer and an agent
    output are held to one standard -- though an answer is read as one a person
    authored, not one a node produced (`schemas_v3.read_authored_instance_document`):
    only silence and broken bytes are left for `APPROVAL_SCHEMA` to refuse.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    refused = answer(started, workflow, rejected)

    assert isinstance(refused, UnanswerableWait), refused
    with started.engine.connect() as connection:
        stored = connection.scalar(sa.select(sa.func.count()).select_from(wait_answers))
        state = connection.scalar(
            sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
        )
    assert stored == 0
    assert str(state) == RunState.WAITING_INPUT.value
    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)


@pytest.mark.parametrize(
    "raw_text",
    [b"41", b'{"verdict": "approved"}', b"approved"],
    ids=["a bare number's digits", "a bare object's braces", "an unquoted word"],
)
@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_authored_string_answer_is_admitted_as_the_raw_text_typed(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
    raw_text: bytes,
) -> None:
    """A `\"string\"`-typed wait no longer demands the person quote their answer.

    Before #1091 only a JSON-quoted string was admitted, so `41` read as the
    JSON number 41 (refused) rather than the two-character answer a person who
    typed it meant. The bytes typed ARE the string now, so all three of these
    -- shapes that used to look like a JSON number, object or bare word -- are
    admitted as exactly the text a person wrote.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, raw_text), AnswerAcceptedPending)


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answer_to_a_v3_turn_that_has_already_been_answered_is_idempotent(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The same bytes for an applied execution report that answer; different bytes
    are a conflict that leaves the applied answer standing."""
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)
    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)
    wait_for_state(started, RunState.COMPLETED)

    late = answer(started, workflow, ANSWER)
    contradicting = answer(started, workflow, b'"rejected"')

    assert isinstance(late, AnswerExistingApplied), late
    assert contradicting == AnswerStateConflict()
    with started.engine.connect() as connection:
        stored = connection.execute(
            sa.select(wait_answers.c.state, wait_answers.c.answer_bytes)
        ).all()
    assert stored == [(WaitAnswerState.APPLIED.value, ANSWER)]


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param((ANSWER, ANSWER), id="the same bytes"),
        pytest.param((ANSWER, b'"rejected"'), id="different bytes"),
    ],
)
@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_racing_answers_leave_one_durable_answer_and_one_heir(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
    answers: tuple[bytes, bytes],
) -> None:
    """Two people answering the same pause at once cannot double or fork it.

    Two submissions released together: the same bytes are both accepted as the
    one answer, while different bytes leave exactly one winner and hand the
    loser a refusal rather than a second row. Either way the store holds one
    `wait_answers` row and one answer workflow, and the line finishes on the
    one accepted answer -- the only place this door is proven under actual
    concurrency.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)
    barrier = Barrier(2)

    def submit(answer_bytes: bytes) -> object:
        barrier.wait(timeout=5)
        return answer(started, workflow, answer_bytes)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, answers))

    snapshots = [
        result.snapshot
        for result in results
        if isinstance(
            result,
            (AnswerAcceptedPending, AnswerExistingPending, AnswerExistingApplied),
        )
    ]
    refusals = [result for result in results if isinstance(result, AnswerStateConflict)]
    if answers[0] == answers[1]:
        assert (len(snapshots), len(refusals)) == (2, 0)
    else:
        assert (len(snapshots), len(refusals)) == (1, 1)
    accepted = {snapshot.answer.answer_bytes for snapshot in snapshots}
    assert len(accepted) == 1
    assert accepted.issubset(set(answers))

    wait_for_state(started, RunState.COMPLETED)
    with started.engine.connect() as connection:
        stored = connection.scalar(sa.select(sa.func.count()).select_from(wait_answers))
        answer_workflows = connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM workflow_status WHERE name='atelier2_wait_answer'"
            )
        )
    assert (stored, answer_workflows) == (1, 1)


@pytest.mark.parametrize(
    "refused_bytes",
    [
        pytest.param(b"", id="nothing at all"),
        pytest.param(b"\xff", id="bytes that are no text"),
    ],
)
@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_the_store_door_itself_refuses_an_inadmissible_answer_and_writes_nothing(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
    refused_bytes: bytes,
) -> None:
    """What the waiting node admits is the store's to decide, for every caller.

    The refusal is asked of `DbosWaitAnswerer.submit_result` directly as well as
    through the use case, so it is pinned for every caller and not only for the
    one route the API takes -- and it leaves nothing durable behind either way:
    no answer row, no enqueued answer workflow, no event, no moved run.

    A V3 wait has no `answer_type`: its judge is the JSON schema its author
    pinned. Since #1091 reads an answer as the raw text a person typed rather
    than a second JSON encoding of it, a shape that once looked like a
    malformed JSON number (`+5`, `05`, ` 5`, `-0`) is now simply the text those
    characters spell, and `APPROVAL_SCHEMA` admits it; only silence and bytes
    that are not text at all are left refused.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)
    execution = NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE)
    settled_events = durable_events(started)
    with started.engine.connect() as connection:
        settled_run = connection.execute(
            sa.select(
                runs.c.state,
                runs.c.current_node_id,
                runs.c.state_version,
                runs.c.last_event_sequence,
            ).where(runs.c.run_id == RUN.value)
        ).one()

    assert isinstance(answer(started, workflow, refused_bytes), UnanswerableWait)
    direct = DbosWaitAnswerer(
        started.engine, started.settings.application_version
    ).submit_result(
        SubmitWaitAnswerRequest(
            RUN,
            workflow.revision_hash,
            WAIT_NODE,
            execution,
            WaitAnswerActor.OPERATOR,
            refused_bytes,
        )
    )
    assert isinstance(direct, DurableAnswerNotAdmitted)

    assert durable_events(started) == settled_events
    with started.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(wait_answers)) == 0
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status "
                    "WHERE name='atelier2_wait_answer'"
                )
            )
            == 0
        )
        assert (
            connection.execute(
                sa.select(
                    runs.c.state,
                    runs.c.current_node_id,
                    runs.c.state_version,
                    runs.c.last_event_sequence,
                ).where(runs.c.run_id == RUN.value)
            ).one()
            == settled_run
        )


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_pending_answer_reports_itself_again_and_refuses_different_bytes(
    tmp_path: Path,
) -> None:
    """An accepted answer nobody has applied yet stays the one answer.

    The runtime that would apply it is closed first, so the PENDING row is a
    real parked state rather than a timing window. The same bytes submitted
    again are that answer reported back; different bytes are refused rather
    than becoming a second row -- and either way the store still holds exactly
    the first answer, still PENDING.
    """
    recording = recording_provider()
    started = wait_runtime_over(tmp_path, recording)
    started.initialize_storage()
    try:
        workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
        wait_for_state(started, RunState.WAITING_INPUT)
        version = started.settings.application_version
    finally:
        started.close()

    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    try:
        answerer = DbosWaitAnswerer(engine, version)

        def submit(value: bytes) -> object:
            return answer_wait_result(
                RUN,
                workflow.revision_hash,
                WAIT_NODE,
                NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE),
                WaitAnswerActor.OPERATOR,
                value,
                answerer,
            )

        first = submit(ANSWER)
        assert isinstance(first, AnswerAcceptedPending), first

        assert submit(ANSWER) == AnswerExistingPending(first.snapshot)
        assert submit(b'"rejected"') == AnswerStateConflict()

        with engine.connect() as connection:
            stored = connection.execute(
                sa.select(wait_answers.c.state, wait_answers.c.answer_bytes)
            ).all()
    finally:
        engine.dispose()
    assert stored == [(WaitAnswerState.PENDING.value, ANSWER)]


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_duplicate_answer_in_the_committed_transition_window_is_that_answer(
    tmp_path: Path,
) -> None:
    """Between the answer's commit and the heir's first event, the store is healthy.

    In that window the head event still carries the wait node while
    `current_node_id` already names the heir -- the same committed-transition
    state the restart sweep once misread as a dead gap. The front door misread
    it too, handing a duplicate identical answer `DurableStateCorrupt` instead
    of the answer it already accepted. The window is parked here exactly, with
    nothing running: the duplicate must be told its own answer, different bytes
    must still be refused, and neither may write anything.
    """
    recording = recording_provider()
    started = wait_runtime_over(tmp_path, recording)
    started.initialize_storage()
    try:
        workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
        wait_for_state(started, RunState.WAITING_INPUT)
        version = started.settings.application_version
    finally:
        started.close()

    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    try:
        answerer = DbosWaitAnswerer(engine, version)

        def submit(value: bytes) -> object:
            return answer_wait_result(
                RUN,
                workflow.revision_hash,
                WAIT_NODE,
                NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE),
                WaitAnswerActor.OPERATOR,
                value,
                answerer,
            )

        assert isinstance(submit(ANSWER), AnswerAcceptedPending)
        # The apply commits without any runtime to start the heir, so the run
        # now stands STARTED on the heir while the head event is WAIT_ANSWERED
        # on the wait node: the committed-transition window, held open.
        with engine.begin() as connection:
            pending = load_wait_answer(
                connection, RUN, workflow.revision_hash, WAIT_NODE
            )
            commit_wait_answered(connection, pending.answer)
        with engine.connect() as connection:
            parked = connection.execute(
                sa.select(runs.c.state, runs.c.current_node_id).where(
                    runs.c.run_id == RUN.value
                )
            ).one()
        assert parked == (RunState.STARTED.value, "review")

        duplicate = submit(ANSWER)
        assert isinstance(duplicate, AnswerExistingApplied), duplicate
        assert duplicate.snapshot.answer.answer_bytes == ANSWER
        assert submit(b'"rejected"') == AnswerStateConflict()

        with engine.connect() as connection:
            stored = connection.execute(
                sa.select(wait_answers.c.state, wait_answers.c.answer_bytes)
            ).all()
            still_parked = connection.execute(
                sa.select(
                    runs.c.state,
                    runs.c.current_node_id,
                    runs.c.last_event_sequence,
                ).where(runs.c.run_id == RUN.value)
            ).one()
    finally:
        engine.dispose()
    assert stored == [(WaitAnswerState.APPLIED.value, ANSWER)]
    assert still_parked == (RunState.STARTED.value, "review", 3)


CANCEL_KEY = "operator-stops-the-wait-1"


def test_a_run_resting_at_a_wait_ends_cancelled_on_its_own_attestation(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """#668: a pause nobody will answer ends, and the record says who ended it.

    The event the cancel writes is the whole attestation -- there is no attempt
    to stamp -- so it has to be the last thing the run's terminal hash folds
    over, and it has to name the minted command. A lift that closed the run
    without writing one would leave a hash over a log that ends at the pause,
    saying nothing about why the run stopped.
    """
    started, recording = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    ended = cancel(
        started.engine, started.settings.application_version, workflow, CANCEL_KEY
    )

    assert isinstance(ended, CancelEndedRun), ended
    assert ended.run.state is RunState.CANCELLED
    command_id = RunCancelCommandId.for_key(CANCEL_KEY).value
    assert durable_events(started) == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, WAIT_NODE, RunEventKind.WAITING_INPUT.value, b""),
        (3, WAIT_NODE, RunEventKind.WAIT_CANCELLED.value, command_id.encode("utf-8")),
    ]
    with started.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
    assert str(run["state"]) == RunState.CANCELLED.value
    assert str(run["current_node_id"]) == WAIT_NODE
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == ["implement"]


def test_the_same_cancel_command_answers_the_ended_run_again(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A retry after a lost response reads the command's own attestation back.

    The pause holds no attempt row this command could have stamped, so the
    event's payload is the only durable trace of it -- and a second submission
    must find that trace rather than be told the run has simply already ended.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)
    version = started.settings.application_version

    first = cancel(started.engine, version, workflow, CANCEL_KEY)
    second = cancel(started.engine, version, workflow, CANCEL_KEY)

    assert isinstance(first, CancelEndedRun), first
    assert isinstance(second, CancelEndedRun), second
    assert second.run == first.run
    assert [kind for _, _, kind, _ in durable_events(started)].count(
        RunEventKind.WAIT_CANCELLED.value
    ) == 1


def test_a_cancel_fenced_on_another_node_leaves_the_pause_standing(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """#439 D2's fence covers the pause too: a stale confirmation stops nothing.

    The execution named here is the agent node the line already finished, which
    is exactly what an operator's browser would still be holding if it read the
    run before the pause was reached.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    refused = cancel(
        started.engine,
        started.settings.application_version,
        workflow,
        CANCEL_KEY,
        node_id="implement",
    )

    assert refused == CancelNotCancellable(RunCancellationRefusal.BETWEEN_NODES)
    assert [kind for _, _, kind, _ in durable_events(started)] == [
        RunEventKind.AGENT_COMPLETED.value,
        RunEventKind.WAITING_INPUT.value,
    ]


def test_a_cancel_arriving_on_an_accepted_answer_refuses_rather_than_drop_it(
    tmp_path: Path,
) -> None:
    """An accepted message is never silently thrown away by a cancel.

    The answer is taken while nothing is left to apply it -- the runtime that
    would have driven it is closed first -- so the PENDING row this refusal is
    about is a real one an operator was already told had been accepted, not a
    window arranged by timing.
    """
    recording = recording_provider()
    started = wait_runtime_over(tmp_path, recording)
    started.initialize_storage()
    try:
        workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
        wait_for_state(started, RunState.WAITING_INPUT)
        version = started.settings.application_version
    finally:
        started.close()

    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    try:
        accepted = answer_wait_result(
            RUN,
            workflow.revision_hash,
            WAIT_NODE,
            NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE),
            WaitAnswerActor.OPERATOR,
            ANSWER,
            DbosWaitAnswerer(engine, version),
        )
        refused = cancel(engine, version, workflow, CANCEL_KEY)
        with engine.connect() as connection:
            state = connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
            )
            answer_state = connection.scalar(
                sa.select(wait_answers.c.state).where(
                    wait_answers.c.node_execution_id
                    == NodeExecutionId.for_node(
                        RUN, workflow.revision_hash, WAIT_NODE
                    ).value
                )
            )
    finally:
        engine.dispose()

    assert isinstance(accepted, AnswerAcceptedPending), accepted
    assert refused == CancelNotCancellable(RunCancellationRefusal.ANSWER_IN_FLIGHT)
    assert str(state) == RunState.WAITING_INPUT.value
    assert str(answer_state) == WaitAnswerState.PENDING.value


UNSETTLED_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED")
"""DBOS's own words for a workflow that still owes this store an outcome."""

_UNSETTLED_WORKFLOWS = sa.text(
    "SELECT COUNT(*) FROM workflow_status WHERE status IN :unsettled"
).bindparams(sa.bindparam("unsettled", expanding=True))


def wait_for_recovery_to_drain(runtime: DbosRuntime) -> None:
    """Block until nothing this runtime recovered is still owed an outcome.

    `DbosRuntime.launch` is already the barrier for recovery having *happened*:
    it arms DBOS recovery and runs every convergence sweep synchronously under
    its lock before it returns. What it does not wait for is a workflow that
    recovery armed and handed to the queue, which runs afterwards -- so that is
    what is waited on here, by DBOS's own status rather than by a duration.

    A run resting at a pause leaves nothing unsettled, so this returns at once
    today. That is the condition being true, not the check being absent: the
    day recovery does re-arm a Wait node, this holds until that workflow has
    run and the assertions below see what it wrote.
    """
    deadline = time.monotonic() + 8
    unsettled = -1
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            unsettled = int(
                connection.scalar(
                    _UNSETTLED_WORKFLOWS,
                    {"unsettled": list(UNSETTLED_WORKFLOW_STATUSES)},
                )
                or 0
            )
        if unsettled == 0:
            return
        time.sleep(0.025)
    raise AssertionError(f"{unsettled} workflows never settled after recovery")


def test_a_cancelled_pause_stays_ended_when_a_new_runtime_comes_up_over_it(
    tmp_path: Path,
) -> None:
    """Recovery finds a run that ended at its pause and leaves it alone.

    This is the sentence the cancel has to earn beyond its own transaction. A
    pause is durable with nothing polling it, so the process that wrote the
    cancellation is not what keeps the run ended -- the record is. A restarting
    runtime replays whatever DBOS still holds, and a Wait node re-driven here
    would try to write WAITING_INPUT onto a CANCELLED run.

    Nothing here waits out a duration. `launch()` returns only once recovery is
    armed and every convergence sweep has run, and `wait_for_recovery_to_drain`
    then holds until the queue owes nothing -- so the event log compared below
    is the one recovery actually left, not the one a sleep happened to catch.
    """
    recording = recording_provider()
    started = wait_runtime_over(tmp_path, recording)
    started.initialize_storage()
    try:
        workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
        wait_for_state(started, RunState.WAITING_INPUT)
        ended = cancel(
            started.engine, started.settings.application_version, workflow, CANCEL_KEY
        )
        assert isinstance(ended, CancelEndedRun), ended
        events_at_the_cancel = durable_events(started)
        with started.engine.connect() as connection:
            hash_at_the_cancel = connection.scalar(
                sa.select(runs.c.terminal_hash).where(runs.c.run_id == RUN.value)
            )
    finally:
        started.close()

    recovered = wait_runtime_over(tmp_path, recording)
    try:
        recovered.launch()
        wait_for_recovery_to_drain(recovered)
        with recovered.engine.connect() as connection:
            run = (
                connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
                .mappings()
                .one()
            )
        assert durable_events(recovered) == events_at_the_cancel
    finally:
        recovered.close()

    assert str(run["state"]) == RunState.CANCELLED.value
    assert str(run["terminal_hash"]) == str(hash_at_the_cancel)
