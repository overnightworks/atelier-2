"""What a subscriber of one run's events is served over the real HTTP boundary.

Every run this API serves is format 3, so the line these tests stream is one:
two agent nodes and the person who approves what they made. The single V1
fixture left in this file belongs to the receipt-index test at the bottom, which
asks the durable store for a page directly and never reaches the wire.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.sse import ServerSentEvent
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    commit_action_completed,
    commit_wait_answered,
    load_wait_answer,
)
from atelier2.adapters.dbos.run_transitions import (
    commit_reconciliation_required,
    commit_waiting_input,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import effect_intents, run_events
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.api.app import create_app
from atelier2.api.references import encode_public_run_reference
from atelier2.api.stream import (
    BoundedQueryRunner,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectId,
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
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import (
    RunEventPage,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import (
    RunFound,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    dying,
)
from tests.scenarios.api import (
    api_limits,
    durable_ports,
    durable_queries,
    event_poll_backoff,
    stream_page_reader,
)
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    complete_v3_agent_node,
    prepare_and_launch_graph_action,
    publish_pinned_revisions,
    start_published_v3_run,
    submit_reconcile_command,
    submit_wait_answer,
)
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    V3_EFFECT_LINE_ACTION_NODE_ID,
    V3_EFFECT_LINE_AGENT_JOB,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
    V3_EFFECT_LINE_WAIT_NODE_ID,
    declared_output,
)

PROVIDER_OUTPUT = b'"the exact provider bytes"'
DYING_PROVIDER_SAID = b"the provider process said this"
APPROVAL = b'"approved"'
STREAMED_RUN = RunId("v3/sse")
STREAMED_DOCUMENT = (
    b"""format_version: 3
name: Two agents, then a person
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the node before you did.
    depends_on: [implement]
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [review]
"""
    + declared_output(ANY_JSON_SCHEMA, "approval")
)
STREAMED_HISTORY = (
    ("implement", "AGENT_COMPLETED"),
    ("review", "AGENT_COMPLETED"),
    ("approve", "WAITING_INPUT"),
    ("approve", "WAIT_ANSWERED"),
)
"""Every event the streamed line persists, in the order it persists them."""

EVENTS_BEFORE_THE_ANSWER = STREAMED_HISTORY.index(("approve", "WAIT_ANSWERED"))
"""How far the line gets on its own, before a person answers its Wait."""

RECONCILED_ACTION_DOCUMENT = V3_EFFECT_LINE_DOCUMENT
"""The one history here carrying effect receipts, for the durable read below.

Its run never reaches the wire: the receipt-index test asks the durable store
for a page directly, so the effect line is driven by hand up to its answered
Wait rather than through the launched runtime."""


def _agent_runtime(
    tmp_path: Path, application_version: str, factory: RecordingAgentExecutorFactoryV2
) -> DbosRuntime:
    """A runtime holding the executor a V3 line's agent nodes are bound to."""

    runtime = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, application_version, agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (factory,),
    )
    runtime.initialize_storage()
    return runtime


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = _agent_runtime(
        tmp_path,
        "sse-tests",
        RecordingAgentExecutorFactoryV2(
            "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
        ),
    )
    try:
        yield started
    finally:
        started.close()


def _persisted_events(runtime: DbosRuntime, run_id: RunId) -> int:
    with runtime.engine.connect() as connection:
        return int(
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(run_events)
                .where(run_events.c.run_id == run_id.value)
            )
            or 0
        )


def _await_events(runtime: DbosRuntime, run_id: RunId, expected: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _persisted_events(runtime, run_id) >= expected:
            return
        time.sleep(0.025)
    raise AssertionError(
        f"{run_id.value} persisted {_persisted_events(runtime, run_id)} events, "
        f"expected {expected}"
    )


def _publish_schema(runtime: DbosRuntime) -> None:
    published = DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published


def _streamed_history(runtime: DbosRuntime) -> None:
    """Drive the streamed line until every event of `STREAMED_HISTORY` is durable.

    Nothing writes an event by hand: the runtime runs both agent nodes, stops on
    the Wait, and the answer a person submits is what ends the run.
    """

    _publish_schema(runtime)
    revision = WorkflowRevision(STREAMED_DOCUMENT)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        STREAMED_RUN,
        revision,
        runtime.agent_executor_registry,
    )
    runtime.launch()
    _await_events(runtime, STREAMED_RUN, EVENTS_BEFORE_THE_ANSWER)
    submit_wait_answer(
        runtime.engine,
        runtime.settings.application_version,
        SubmitWaitAnswerRequest(
            STREAMED_RUN,
            revision.revision_hash,
            "approve",
            NodeExecutionId.for_node(STREAMED_RUN, revision.revision_hash, "approve"),
            WaitAnswerActor.OPERATOR,
            APPROVAL,
        ),
    )
    _await_events(runtime, STREAMED_RUN, len(STREAMED_HISTORY))


def _failed_first_node(runtime: DbosRuntime, run_id: RunId) -> None:
    """Run the streamed line until its first agent node has failed durably."""

    _publish_schema(runtime)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        run_id,
        WorkflowRevision(STREAMED_DOCUMENT),
        runtime.agent_executor_registry,
    )
    runtime.launch()
    _await_events(runtime, run_id, 1)


def _receipt_history(runtime: DbosRuntime) -> RunId:
    run_id = RunId("sse/run")
    revision = WorkflowRevision(RECONCILED_ACTION_DOCUMENT)
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
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
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        PROVIDER_OUTPUT,
    )
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
            run_id,
            revision.revision_hash,
            V3_EFFECT_LINE_ACTION_NODE_ID,
            intent.request.payload,
        )
    command = ReconcileCommand(
        ReconcileCommandId("command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected exact request",
        OperatorFoundEffect(EffectId("effect"), EffectResult(b"result")),
    )
    submit_reconcile_command(runtime.engine, runtime.settings, command)
    determination = command.determination
    assert isinstance(determination, OperatorFoundEffect)
    with Session(runtime.engine) as session, session.begin():
        commit_resolution(
            session,
            intent.binding.logical_key.value,
            revision.revision_hash.value,
            encode_found(
                PerformedEffect(determination.effect_id, determination.result),
                ConfirmationSource.OPERATOR_FOUND,
                command.command_id,
            ),
            command.command_id,
        )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_action_completed(
            connection, intent.binding.logical_key, revision.revision_hash
        )
        commit_waiting_input(
            connection, run_id, revision.revision_hash, V3_EFFECT_LINE_WAIT_NODE_ID
        )
    answerer = DbosWaitAnswerer(runtime.engine, runtime.settings.application_version)
    answerer.submit_result(
        SubmitWaitAnswerRequest(
            run_id,
            revision.revision_hash,
            V3_EFFECT_LINE_WAIT_NODE_ID,
            NodeExecutionId.for_node(
                run_id, revision.revision_hash, V3_EFFECT_LINE_WAIT_NODE_ID
            ),
            WaitAnswerActor.OPERATOR,
            b"17",
        )
    )
    with canonical_write_transaction(runtime.engine) as connection:
        answer = load_wait_answer(
            connection, run_id, revision.revision_hash, V3_EFFECT_LINE_WAIT_NODE_ID
        )
        commit_wait_answered(connection, answer.answer)
    return run_id


def _client(runtime: DbosRuntime, page_size: int = 2) -> TestClient:
    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=durable_ports(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ),
        limits=api_limits(event_page_size=page_size),
        event_poll_backoff=event_poll_backoff(),
    )
    return TestClient(app)


def _parse_events(body: str) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", maxsplit=1) for line in block.splitlines())
        assert set(fields) == {"id", "data"}
        parsed.append(
            {
                "id": fields["id"],
                "data": json.loads(fields["data"]),
            }
        )
    return parsed


def _field(streamed: dict[str, object], name: str) -> object:
    return cast(dict[str, object], streamed["data"])[name]


def _history_of(events: list[dict[str, object]]) -> tuple[tuple[object, object], ...]:
    return tuple(
        (_field(streamed, "node_id"), _field(streamed, "event")) for streamed in events
    )


def _streamed_path() -> str:
    reference = encode_public_run_reference(STREAMED_RUN)
    return f"/atelier/api/v1/runs/{reference}/events"


def test_agent_failed_stream_is_bounded_and_secret_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canary = "private-agent-secret-41b8e0"
    factory = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "controlled-process",
        b"unused",
        command=dying(1, DYING_PROVIDER_SAID),
    )
    factory.__dict__["private_material"] = canary
    runtime = _agent_runtime(tmp_path, "failed-sse", factory)
    try:
        caplog.set_level(logging.DEBUG)
        run_id = RunId("v3/failed-sse")
        _failed_first_node(runtime, run_id)
        queries = durable_queries(runtime.engine)

        found = queries.get_run(run_id)
        assert isinstance(found, RunFound)

        async def first_event() -> ServerSentEvent:
            stream = aiter(
                stream_server_events(
                    PreparedEventStream(run_id, 0, 1, True, found.projection),
                    stream_page_reader(queries),
                    BoundedQueryRunner(1, admission_timeout_seconds=1),
                    page_size=PageLimit(1),
                    limits=api_limits(),
                    poll_backoff=event_poll_backoff(),
                )
            )
            return await anext(stream)

        streamed = asyncio.run(first_event())
        assert streamed.event is None
        stream_json = streamed.model_dump_json()
        run_json = (
            _client(runtime)
            .get("/atelier/api/v1/runs/" + encode_public_run_reference(run_id))
            .text
        )
        with runtime.engine.connect() as connection:
            durable = {
                table: tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(f'SELECT * FROM "{table}"')
                )
                for table in sa.inspect(connection).get_table_names()
            }

        failure_code = AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY.value
        assert '"event":"AGENT_FAILED"' in stream_json
        assert f'"failure_code":"{failure_code}"' in stream_json
        assert all(
            canary not in channel
            for channel in (
                repr(durable),
                run_json,
                stream_json,
                "\n".join(record.getMessage() for record in caplog.records),
            )
        )
        assert "payload" not in stream_json
        assert "base64" not in stream_json
        assert "stderr" not in stream_json
    finally:
        runtime.close()


def test_sse_emits_every_persisted_event_and_resumes_after_its_cursor(
    runtime: DbosRuntime,
) -> None:
    _streamed_history(runtime)
    client = _client(runtime)
    path = _streamed_path()

    first = _parse_events(client.get(path).text)
    acknowledged = first[EVENTS_BEFORE_THE_ANSWER - 1]
    resumed = _parse_events(
        client.get(path, headers={"last-event-id": str(acknowledged["id"])}).text
    )
    second_reader = _parse_events(client.get(path).text)

    assert _history_of(first) == STREAMED_HISTORY
    assert [streamed["id"] for streamed in first] == [
        _field(streamed, "cursor") for streamed in first
    ]
    assert {_field(streamed, "workflow_format_version") for streamed in first} == {3}
    # The bytes a person sent come back out of the stream unchanged, spelled
    # rather than recomputed: asking the answer for its own base64 and hash
    # would compare the frame with the value that made it.
    answered = first[STREAMED_HISTORY.index(("approve", "WAIT_ANSWERED"))]
    assert _field(answered, "answer_base64") == "ImFwcHJvdmVkIg=="
    assert _field(answered, "answer_hash") == (
        "70ab47ec8fbd75027e36d5fae28639b51de6e7265220723cc99156dca295fcc5"
    )
    assert _field(answered, "actor") == "operator"
    unacknowledged = first[EVENTS_BEFORE_THE_ANSWER:]
    # A resume is compared by event identity rather than whole frame: the node
    # rail a frame carries is folded from the events its own response streamed,
    # so a resume legitimately says less about the nodes it did not repeat.
    assert [streamed["id"] for streamed in resumed] == [
        streamed["id"] for streamed in unacknowledged
    ]
    assert [_field(streamed, "event_hash") for streamed in resumed] == [
        _field(streamed, "event_hash") for streamed in unacknowledged
    ]
    assert second_reader == first


def test_two_concurrent_readers_receive_the_same_durable_order(
    runtime: DbosRuntime,
) -> None:
    _streamed_history(runtime)
    path = _streamed_path()
    barrier = Barrier(2)

    def read(
        _: int,
        rendezvous: Barrier = barrier,
        configured_runtime: DbosRuntime = runtime,
        request_path: str = path,
    ) -> list[dict[str, object]]:
        rendezvous.wait(timeout=5)
        return _parse_events(_client(configured_runtime).get(request_path).text)

    with ThreadPoolExecutor(max_workers=2) as pool:
        histories = list(pool.map(read, range(2)))

    assert histories[0] == histories[1]
    assert [_field(streamed, "sequence") for streamed in histories[0]] == list(
        range(1, len(STREAMED_HISTORY) + 1)
    )


def test_receipt_result_limit_stays_in_each_indexed_snapshot_query(
    runtime: DbosRuntime,
) -> None:
    run_id = _receipt_history(runtime)
    receipt_selects: list[tuple[str, tuple[Any, ...]]] = []
    connection_ids: set[int] = set()
    transaction_states: list[bool] = []

    def capture_receipt_select(
        connection: Any,
        _cursor: Any,
        statement: str,
        parameters: tuple[Any, ...],
        _context: Any,
        _executemany: bool,
        captured_receipt_selects: list[tuple[str, tuple[Any, ...]]] = (receipt_selects),
        captured_connection_ids: set[int] = connection_ids,
        captured_transaction_states: list[bool] = transaction_states,
    ) -> None:
        if "FROM effect_receipts" not in statement:
            return
        captured_receipt_selects.append((statement, parameters))
        raw = connection.connection.driver_connection
        captured_connection_ids.add(id(raw))
        captured_transaction_states.append(bool(raw.in_transaction))

    event.listen(runtime.engine, "before_cursor_execute", capture_receipt_select)
    try:
        page = durable_queries(runtime.engine).read_run_event_page(run_id, 2, 2)
    finally:
        event.remove(runtime.engine, "before_cursor_execute", capture_receipt_select)

    assert isinstance(page, RunEventPage)
    assert len(receipt_selects) == 2
    assert connection_ids
    assert len(connection_ids) == 1
    assert transaction_states == [True, True]
    for statement, parameters in receipt_selects:
        assert "THEN effect_receipts.result END AS result" in statement
        assert "length(effect_receipts.result) AS _atelier_length_result" in statement
        with runtime.engine.connect() as connection:
            plan = tuple(
                str(record[-1]).upper()
                for record in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                )
            )
        assert any("SEARCH EFFECT_RECEIPTS USING INDEX" in detail for detail in plan)
        assert all("SCAN" not in detail for detail in plan)
