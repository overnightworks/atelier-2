"""A declared loop may now repeat a Wait node, and a run may start on one.

**Why this file exists.** #658 amends `_unrepeatable_loop_forms` to admit a Wait
inside a loop body -- no grammar changed, because the round identity this needs
already landed with ADR 0014 (`WaitNodeBinding` carries the round ordinal, and an
answer is keyed by execution and round). What was never driven end to end is the
shape a conversation actually needs: a generic `loop{wait, agent}` document, run
through the public start and answer doors, where the run's very first node is the
Wait a person answers, an agent reads that answer plus its own previous round's
report, and the loop ends honestly at its declared ceiling.

**Why one test.** The five things this proves -- start-on-Wait, the answer as a
same-round input, the previous-round self-edge (present in round two, absent in
round one), the ceiling ending the run rather than a verdict, and a restart across
the second round's pause -- only add up to the claimed shape together. Proving them
apart would let four pass while the fifth silently regressed.

Running it is what found the store gap the V36 hop closes: the once-per-node
event key refused a Wait node's second pause, so a document shaped like this one
could be admitted but not driven to a second round.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import run_events, runs, wait_answers
from atelier2.api.app import create_app
from atelier2.api.references import encode_public_run_reference
from atelier2.application.answer_wait import AnswerAcceptedPending, answer_wait_result
from atelier2.application.compose_node_job import RESULT_HEADING
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    WaitAnswerActor,
)
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    answering_each_execution,
    publish_checked_model_registry,
)
from tests.scenarios.api import (
    api_limits,
    durable_ports,
    durable_queries,
    event_poll_backoff,
)
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/wait-in-loop")
WAIT_NODE = "ask"
AGENT_NODE = "work"
LOOP_ID = "conversation"
LOOP_MAXIMUM_ROUNDS = 3

ANSWER_ROUND_1 = b'"do the first thing"'
ANSWER_ROUND_2 = b'"do the second thing"'
ANSWER_ROUND_3 = b'"do the third thing"'
REPORT_ROUND_1 = b'"the first thing is done"'
REPORT_ROUND_2 = b'"the second thing is done"'
REPORT_ROUND_3 = b'"the third thing is done"'
SOURCE_NODE = "source"
LATER_FIRST_NODE = "later-first"
LATER_LAST_NODE = "later-last"
SOURCE_OUTPUT = b'"the durable source value"'

WAIT_IN_LOOP_DOCUMENT = (
    b"""format_version: 3
name: A person and an agent take alternating rounds
nodes:
  - id: """
    + WAIT_NODE.encode()
    + b"""
    type: wait
    prompt: What should this round do?
"""
    + declared_output(ANY_JSON_SCHEMA, "answer")
    + b"""  - id: """
    + AGENT_NODE.encode()
    + b"""
    type: agent
    role: builder
    mode: headless
    instruction: Do what the person just asked, informed by your own last report.
    depends_on: ["""
    + WAIT_NODE.encode()
    + b"""]
    inputs:
      - name: answer
        from: {node: """
    + WAIT_NODE.encode()
    + b""", output: answer}
      - name: previous_report
        from: {node: """
    + AGENT_NODE.encode()
    + b""", output: report}
"""
    + declared_output(ANY_JSON_SCHEMA, "report")
    + f"""loops:
  - id: {LOOP_ID}
    body: [{WAIT_NODE}, {AGENT_NODE}]
    maximum_rounds: {LOOP_MAXIMUM_ROUNDS}
""".encode()
)
"""The thinnest shape a conversational round needs: a person, then an agent that
reads what the person just said and what it itself said last round -- looped, with
no verdict, so only the declared ceiling ends it.

The loop enters at the Wait, which is also the whole document's only root: this is
the run that has never been started before, because every earlier Wait test put an
Agent in front of it.
"""

WAIT_BEFORE_A_LATER_LOOP_DOCUMENT = (
    b"""format_version: 3
name: A bound wait remains readable after a later loop advances the round
nodes:
  - id: """
    + SOURCE_NODE.encode()
    + b"""
    type: agent
    role: builder
    mode: headless
    instruction: Produce the source value.
"""
    + declared_output(ANY_JSON_SCHEMA, "report")
    + b"""  - id: """
    + WAIT_NODE.encode()
    + b"""
    type: wait
    prompt: What should happen to this source?
    depends_on: ["""
    + SOURCE_NODE.encode()
    + b"""]
    inputs:
      - name: source
        from: {node: """
    + SOURCE_NODE.encode()
    + b""", output: report}
"""
    + declared_output(ANY_JSON_SCHEMA, "answer")
    + b"""  - id: """
    + AGENT_NODE.encode()
    + b"""
    type: agent
    role: builder
    mode: headless
    instruction: Apply the person's answer.
    depends_on: ["""
    + WAIT_NODE.encode()
    + b"""]
"""
    + declared_output(ANY_JSON_SCHEMA, "report")
    + b"""  - id: """
    + LATER_FIRST_NODE.encode()
    + b"""
    type: agent
    role: builder
    mode: headless
    instruction: Begin the later loop round.
    depends_on: ["""
    + AGENT_NODE.encode()
    + b"""]
"""
    + declared_output(ANY_JSON_SCHEMA, "report")
    + b"""  - id: """
    + LATER_LAST_NODE.encode()
    + b"""
    type: agent
    role: builder
    mode: headless
    instruction: End the later loop round.
    depends_on: ["""
    + LATER_FIRST_NODE.encode()
    + b"""]
"""
    + declared_output(ANY_JSON_SCHEMA, "report")
    + f"""loops:
  - id: {LOOP_ID}
    body: [{WAIT_NODE}, {AGENT_NODE}]
    maximum_rounds: 2
  - id: later
    body: [{LATER_FIRST_NODE}, {LATER_LAST_NODE}]
    maximum_rounds: 3
""".encode()
)


def provider() -> RecordingAgentExecutorFactoryV2:
    """The agent executor, answering each round of `work` with its own report."""
    return RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        REPORT_ROUND_1,
        command=answering_each_execution(
            {
                (AGENT_NODE, 1): REPORT_ROUND_1,
                (AGENT_NODE, 2): REPORT_ROUND_2,
                (AGENT_NODE, 3): REPORT_ROUND_3,
            }
        ),
    )


def runtime_over(root: Path, recording: RecordingAgentExecutorFactoryV2) -> DbosRuntime:
    """A runtime over the durable state in this directory, as a restart builds one."""
    return DbosRuntime(
        canonical_runtime_settings(
            root, "v3-wait-in-loop-test", agent_scratch_root(root)
        ),
        canonical_loopback_effects(root),
        (recording,),
    )


@pytest.fixture
def recording() -> RecordingAgentExecutorFactoryV2:
    return provider()


def publish_and_start(
    runtime: DbosRuntime,
    recording: RecordingAgentExecutorFactoryV2,
    document: bytes = WAIT_IN_LOOP_DOCUMENT,
) -> WorkflowRevision:
    """Publish the document and its bindings, then start the run through the
    public start seam -- nothing here reaches into the engine."""
    del recording
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
    client = public_client(runtime)
    published_response = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )
    assert published_response.status_code in (200, 201), published_response.text
    started_response = client.post(
        "/atelier/api/v1/runs",
        json={
            "workflow_format_version": 3,
            "run_id": RUN.value,
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": configuration.revision_hash.value,
                }
            ],
            "orders": [],
        },
    )
    assert started_response.status_code == 201, started_response.text
    return workflow


def public_client(runtime: DbosRuntime) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=durable_ports(
                runtime.engine,
                runtime.settings,
                runtime.agent_executor_registry,
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def public_answer(
    client: TestClient,
    workflow: WorkflowRevision,
    value: bytes,
    round_ordinal: int,
) -> Response:
    execution_id = NodeExecutionId.for_node(
        RUN, workflow.revision_hash, WAIT_NODE, round_ordinal
    )
    return client.post(
        f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}/answers",
        json={
            "workflow_revision_hash": workflow.revision_hash.value,
            "node_id": WAIT_NODE,
            "expected_node_execution_id": execution_id.value,
            "actor": "operator",
            "answer_base64": base64.b64encode(value).decode("ascii"),
        },
    )


def answer(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    value: bytes,
    round_ordinal: int = 1,
) -> object:
    """Answer the currently open round of the Wait, exactly as the API route does."""
    return answer_wait_result(
        RUN,
        workflow.revision_hash,
        WAIT_NODE,
        NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE, round_ordinal),
        WaitAnswerActor.OPERATOR,
        value,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )


def wait_for_waiting_round(runtime: DbosRuntime, round_ordinal: int) -> None:
    """Poll durable truth for the run paused on the Wait in exactly this round.

    A state string alone cannot tell round one's pause from round two's -- both
    read WAITING_INPUT -- so this is the predicate a caller actually needs.
    """
    deadline = time.monotonic() + 12
    observed: tuple[str, int | None] = ("", None)
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            row = connection.execute(
                sa.select(runs.c.state, runs.c.current_round_ordinal).where(
                    runs.c.run_id == RUN.value
                )
            ).one()
        observed = (str(row.state), int(row.current_round_ordinal))
        if observed == (RunState.WAITING_INPUT.value, round_ordinal):
            return
        time.sleep(0.025)
    raise AssertionError(
        f"run stayed {observed!r}, expected waiting in round {round_ordinal}"
    )


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 12
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def durable_events(runtime: DbosRuntime) -> list[tuple[str, int, str]]:
    with runtime.engine.connect() as connection:
        return [
            (str(record.node_id), int(record.round_ordinal), str(record.event_kind))
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            )
        ]


@pytest.mark.proves("one-run-keeps-three-attributed-turns-and-classifies-repeats")
def test_one_public_run_keeps_three_attributed_turns_and_classifies_repeats(
    tmp_path: Path,
    recording: RecordingAgentExecutorFactoryV2,
    dbos_logging_isolation: None,
) -> None:
    started = runtime_over(tmp_path, recording)
    started.initialize_storage()
    try:
        workflow = publish_and_start(started, recording)
        client = public_client(started)
        started.launch()

        # Round one: the run's very first node is the Wait itself -- nothing
        # ran before it, because there is nothing else in this document.
        wait_for_waiting_round(started, 1)
        assert durable_events(started) == [
            (WAIT_NODE, 1, RunEventKind.WAITING_INPUT.value)
        ]

        first = public_answer(client, workflow, ANSWER_ROUND_1, 1)
        assert first.status_code == 202, first.text

        # Round two's own pause is durable proof round one's agent ran and the
        # loop turned back to its head rather than ending on the bound alone.
        wait_for_waiting_round(started, 2)
        assert recording.opened is not None
        round_one_job = recording.opened.requests[0].job_bytes.decode("utf-8")
    finally:
        started.close()

    # The round-one job read the person's answer this round wrote, in the same
    # round `depends_on` already orders -- and nothing of a previous round,
    # because round one has none.
    assert RESULT_HEADING.format(node=WAIT_NODE, name="answer") in round_one_job
    assert ANSWER_ROUND_1.decode("utf-8") in round_one_job
    assert RESULT_HEADING.format(node=AGENT_NODE, name="report") not in round_one_job

    # The process that takes round two's answer, and drives the node reading
    # it, is not the one that held round one's pause.
    recovered = runtime_over(tmp_path, recording)
    try:
        recovered_client = public_client(recovered)
        round_two_read = recovered_client.get(
            f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}"
        )
        assert round_two_read.status_code == 200, round_two_read.text
        assert round_two_read.json()["current_node_execution_id"] == (
            NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE, 2).value
        )
        assert (
            next(
                entry
                for entry in round_two_read.json()["node_rail"]
                if entry["node_id"] == WAIT_NODE
            )["state"]
            == "needs_you"
        )
        before_repeat = durable_events(recovered)
        repeated = public_answer(recovered_client, workflow, ANSWER_ROUND_1, 1)
        assert repeated.status_code == 200, repeated.text
        assert durable_events(recovered) == before_repeat
        changed = public_answer(recovered_client, workflow, ANSWER_ROUND_2, 1)
        assert changed.status_code == 409, changed.text
        assert changed.json()["type"].endswith(":answer-state-conflict")
        assert durable_events(recovered) == before_repeat
        with recovered.engine.connect() as connection:
            assert connection.execute(
                sa.select(wait_answers.c.round_ordinal, wait_answers.c.answer_bytes)
            ).all() == [(1, ANSWER_ROUND_1)]

        second = public_answer(recovered_client, workflow, ANSWER_ROUND_2, 2)
        assert second.status_code == 202, second.text
        recovered.launch()
        wait_for_waiting_round(recovered, 3)
        third = public_answer(recovered_client, workflow, ANSWER_ROUND_3, 3)
        assert third.status_code == 202, third.text
        wait_for_state(recovered, RunState.COMPLETED)

        assert recording.opened is not None
        round_two_job = recording.opened.requests[0].job_bytes.decode("utf-8")

        # The previous-round self-edge: round two's agent reads its own round-one
        # report, exactly the value that runtime produced, under the identity the
        # answer path now carries.
        assert RESULT_HEADING.format(node=WAIT_NODE, name="answer") in round_two_job
        assert ANSWER_ROUND_2.decode("utf-8") in round_two_job
        assert RESULT_HEADING.format(node=AGENT_NODE, name="report") in round_two_job
        assert REPORT_ROUND_1.decode("utf-8") in round_two_job

        # The loop's declared ceiling, not a verdict, ended the run: three rounds
        # ran, each leaving its own Wait and Agent evidence, and nothing after
        # the loop was ever declared to run a third.
        with recovered.engine.connect() as connection:
            head = (
                connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
                .mappings()
                .one()
            )
        assert str(head["state"]) == RunState.COMPLETED.value
        assert int(head["current_round_ordinal"]) == LOOP_MAXIMUM_ROUNDS
        assert str(head["current_node_id"]) == AGENT_NODE
        assert durable_events(recovered) == [
            (WAIT_NODE, 1, RunEventKind.WAITING_INPUT.value),
            (WAIT_NODE, 1, RunEventKind.WAIT_ANSWERED.value),
            (AGENT_NODE, 1, RunEventKind.AGENT_COMPLETED.value),
            (WAIT_NODE, 2, RunEventKind.WAITING_INPUT.value),
            (WAIT_NODE, 2, RunEventKind.WAIT_ANSWERED.value),
            (AGENT_NODE, 2, RunEventKind.AGENT_COMPLETED.value),
            (WAIT_NODE, 3, RunEventKind.WAITING_INPUT.value),
            (WAIT_NODE, 3, RunEventKind.WAIT_ANSWERED.value),
            (AGENT_NODE, 3, RunEventKind.AGENT_COMPLETED.value),
        ]
        assert NodeExecutionId.for_node(
            RUN, workflow.revision_hash, WAIT_NODE, 1
        ) != NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE, 2)
    finally:
        recovered.close()

    reader = runtime_over(tmp_path, recording)
    try:
        reader_client = public_client(reader)
        public_reference = encode_public_run_reference(RUN)
        reread = reader_client.get(f"/atelier/api/v1/runs/{public_reference}")
        assert reread.status_code == 200, reread.text
        assert reread.json()["run_id"] == RUN.value
        assert reread.json()["state"] == RunState.COMPLETED.value

        event_response = reader_client.get(
            f"/atelier/api/v1/runs/{public_reference}/events"
        )
        assert event_response.status_code == 200, event_response.text
        receipts = [
            json.loads(line.removeprefix("data: "))
            for line in event_response.text.splitlines()
            if line.startswith("data: ") and '"event":"WAIT_ANSWERED"' in line
        ]
        assert [receipt["actor"] for receipt in receipts] == ["operator"] * 3
        assert [receipt["node_execution_id"] for receipt in receipts] == [
            NodeExecutionId.for_node(
                RUN, workflow.revision_hash, WAIT_NODE, round_ordinal
            ).value
            for round_ordinal in (1, 2, 3)
        ]
        with reader.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    wait_answers.c.actor,
                    wait_answers.c.node_execution_id,
                    wait_answers.c.round_ordinal,
                )
                .where(wait_answers.c.run_id == RUN.value)
                .order_by(wait_answers.c.round_ordinal)
            ).all()
        assert rows == [
            (
                "operator",
                NodeExecutionId.for_node(
                    RUN, workflow.revision_hash, WAIT_NODE, round_ordinal
                ).value,
                round_ordinal,
            )
            for round_ordinal in (1, 2, 3)
        ]

        with reader.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER wait_answers_payload_no_update")
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                wait_answers.update()
                .where(wait_answers.c.round_ordinal == 2)
                .values(actor=None)
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

        missing_actor_stream = reader_client.get(
            f"/atelier/api/v1/runs/{public_reference}/events"
        )
        missing_actor_frames = [
            json.loads(line.removeprefix("data: "))
            for line in missing_actor_stream.text.splitlines()
            if line.startswith("data: ")
        ]
        assert missing_actor_stream.status_code == 200
        assert missing_actor_frames[-1]["event"] == "STREAM_FAILED"
        assert missing_actor_frames[-1]["problem"]["type"].endswith(
            ":durable-state-corrupt"
        )

        with reader.engine.begin() as connection:
            connection.execute(
                wait_answers.update()
                .where(wait_answers.c.round_ordinal == 2)
                .values(actor=WaitAnswerActor.OPERATOR.value)
            )
            connection.exec_driver_sql("DROP TRIGGER wait_answers_no_delete")
            connection.execute(
                wait_answers.delete().where(
                    wait_answers.c.node_execution_id
                    == NodeExecutionId.for_node(
                        RUN, workflow.revision_hash, WAIT_NODE, 2
                    ).value
                )
            )

        corrupt_stream = reader_client.get(
            f"/atelier/api/v1/runs/{public_reference}/events"
        )
        corrupt_frames = [
            json.loads(line.removeprefix("data: "))
            for line in corrupt_stream.text.splitlines()
            if line.startswith("data: ")
        ]
        assert corrupt_stream.status_code == 200
        assert corrupt_frames[-1]["event"] == "STREAM_FAILED"
        assert corrupt_frames[-1]["problem"]["type"].endswith(":durable-state-corrupt")
    finally:
        reader.close()


def test_a_bound_wait_keeps_its_execution_after_a_later_loop_turns(
    tmp_path: Path,
    recording: RecordingAgentExecutorFactoryV2,
    dbos_logging_isolation: None,
) -> None:
    """A later loop's round cannot change which durable pause detail displays."""
    recording.command = answering_each_execution(
        {
            (SOURCE_NODE, 1): SOURCE_OUTPUT,
            (AGENT_NODE, 1): REPORT_ROUND_1,
            (AGENT_NODE, 2): REPORT_ROUND_2,
            (LATER_FIRST_NODE, 2): REPORT_ROUND_1,
            (LATER_LAST_NODE, 2): REPORT_ROUND_1,
            (LATER_FIRST_NODE, 3): REPORT_ROUND_2,
            (LATER_LAST_NODE, 3): REPORT_ROUND_2,
        }
    )
    runtime = runtime_over(tmp_path, recording)
    runtime.initialize_storage()
    try:
        workflow = publish_and_start(
            runtime, recording, WAIT_BEFORE_A_LATER_LOOP_DOCUMENT
        )
        runtime.launch()
        wait_for_waiting_round(runtime, 1)
        assert isinstance(
            answer(runtime, workflow, ANSWER_ROUND_1), AnswerAcceptedPending
        )
        wait_for_waiting_round(runtime, 2)

        paused = durable_queries(runtime.engine).get_node_detail(RUN, WAIT_NODE)
        assert isinstance(paused, NodeDetailFound), paused
        assert paused.detail.job is not None
        assert SOURCE_OUTPUT.decode("utf-8") in paused.detail.job.decode("utf-8")

        assert isinstance(
            answer(runtime, workflow, ANSWER_ROUND_2, 2), AnswerAcceptedPending
        )
        wait_for_state(runtime, RunState.COMPLETED)

        found = durable_queries(runtime.engine).get_node_detail(RUN, WAIT_NODE)
        assert isinstance(found, NodeDetailFound), found
        assert found.detail.state is NodeState.SUCCEEDED
        assert found.detail.job == paused.detail.job
        assert found.detail.job_hash == paused.detail.job_hash
        assert found.detail.answer is not None
        assert found.detail.answer.value == ANSWER_ROUND_2
        assert found.detail.started_at is not None
        assert found.detail.ended_at == found.detail.started_at
        with runtime.engine.connect() as connection:
            current_round = connection.scalar(
                sa.select(runs.c.current_round_ordinal).where(
                    runs.c.run_id == RUN.value
                )
            )
        assert current_round is not None
        assert int(current_round) == 3
    finally:
        runtime.close()
