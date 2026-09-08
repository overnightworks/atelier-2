from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.sse import ServerSentEvent
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from atelier2.adapters.dbos import queries as queries_module
from atelier2.adapters.dbos.agent_attempt_store import (
    DbosAgentAttemptStore,
    compose_agent_node_job_for_attempt,
)
from atelier2.adapters.dbos.artifact_store import keep_artifact
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.run_transitions import (
    _insert_event,
    event_from_record,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    context_packages_v3,
    effect_intents,
    initialize_schema,
    node_execution_requests_v3,
    node_receipts_v3,
    reconcile_commands,
    run_agent_bindings,
    run_configuration_revisions,
    run_events,
    run_inputs_v3,
    runs,
    workflow_revisions,
)
from atelier2.api import stream as stream_module
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.stream import (
    BoundedQueryRunner,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptReplacement,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionHash,
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentRole,
)
from atelier2.contracts.artifacts import Artifact
from atelier2.contracts.effects import EffectIntentState, ReconcileCommandState
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    logical_effect_key_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_projections import (
    DefectiveRunProjection,
    RunPage,
    RunProjectionProblemCode,
)
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.stored_node_receipt_reasons import store_node_receipt_reason
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    WorkflowRevisionPage,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_events import (
    EventHistoryCorrupt,
    RunEventPage,
    StreamReady,
)
from atelier2.ports.run_queries import (
    RunFound,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowRevisionFound,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    failing_agent_executor_factory,
)
from tests.scenarios.api import (
    api_limits,
    api_ports,
    durable_queries,
    event_poll_backoff,
    healthy_runs,
    permissive_projection_limit,
    stream_page_reader,
    stream_run_projection,
)
from tests.scenarios.runs import publish_v3_agent_bindings
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

# A checkout the pool cannot serve at once is refused without waiting at all.
# Zero is what makes the refusal a decision rather than an elapsed measurement,
# so the test needs no clock to say which bound governed.
NO_WAIT_FOR_A_POOLED_CONNECTION = 0.0


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    configured = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(configured)
    try:
        yield configured
    finally:
        configured.dispose()


class CountingMonotonic:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return 0.0


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _v3_workflow_document(output: str = "result") -> bytes:
    """The document a seeded run stands on, in the format the API serves.

    Its role is the one `publish_v3_agent_bindings` binds, because a run whose
    graph names a role its binding set does not is refused before any read.
    """

    return (
        f"""format_version: 3
name: One agent, seeded for {output}
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: Produce the {output} this seeded run stands for.
""".encode()
        + declared_output(ANY_JSON_SCHEMA, output)
    )


def _workflow_document(output: str = "result") -> bytes:
    """The format-1 document the durable-query seeds still stand on.

    The rows those tests measure are read by the store, never projected onto the
    wire, so they keep the cheaper document. #901 slice 5 collapses the two.
    """

    return f"""format_version: 1
start: agent
nodes:
  - {{id: agent, type: agent, job: test, output: {output}, next: final}}
  - {{id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}}
""".encode()


def test_current_attempt_projection_is_exact_or_fails_as_corrupt(
    tmp_path: Path,
) -> None:
    from tests.integration.test_agent_attempts import attempt_request, attempt_runtime

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/query-integrity")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))
        found = durable_queries(runtime.engine).get_run(request.run_id)
        assert isinstance(found, RunFound)
        assert found.projection.current_agent_attempt is not None

        forged_hash = AgentExecutionRequestHash("f" * 64)
        forged_attempt_id = AgentAttemptId.for_execution(
            request.node_execution_id, forged_hash
        )
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER agent_attempts_state_transition")
            connection.execute(
                agent_attempts.update().values(
                    request_hash=forged_hash.value,
                    attempt_id=forged_attempt_id.value,
                )
            )

        assert isinstance(
            durable_queries(runtime.engine).get_run(request.run_id),
            QueryDurableStateCorrupt,
        )
    finally:
        runtime.close()


def _seed_history(
    engine: Engine,
    *,
    run_id: RunId,
    head: int,
    missing_sequences: frozenset[int] = frozenset(),
    state: RunState = RunState.STARTED,
    terminal_event: bool = False,
    workflow_format_version: int = 1,
    sink_node_id: str | None = None,
    head_event_kind: RunEventKind | None = None,
    head_event_payload: bytes | None = None,
) -> WorkflowRevision:
    """One durable history of the exact shape a run of that family leaves.

    `sink_node_id` is what a V3 run stands on when it ends: its agent sink, whose
    completion carries the same kind as every other node's, so the node and the
    kind together say which event ended the run.

    `head_event_kind` writes another kind at the head, which is what an attempt
    event leaves behind: the store advances the run's head without moving it off
    the node it stands on.
    """
    revision = WorkflowRevision(_workflow_document())
    event_records = []
    for sequence in range(1, head + 1):
        if sequence in missing_sequences:
            continue
        if sequence == head and head_event_kind is not None:
            event_kind = head_event_kind
        elif terminal_event and sequence == head:
            event_kind = RunEventKind.SUBWORKFLOW_COMPLETED
        else:
            event_kind = RunEventKind.AGENT_COMPLETED
        cancellation = event_kind in {
            RunEventKind.AGENT_CANCEL_REQUESTED,
            RunEventKind.AGENT_CANCELLED,
            RunEventKind.AGENT_INTERRUPTED,
        }
        payload = (
            head_event_payload
            if sequence == head and head_event_payload is not None
            else str(sequence).encode()
        )
        node_id = (
            sink_node_id
            if sink_node_id is not None and sequence == head
            else f"node-{sequence}"
        )
        # Built through the production record and inserted as it stores itself.
        # A seeded row with an invented execution identity or event hash is
        # refused by the event contract before any reader can judge it, so a test
        # written that way proves the fixture rather than the read.
        attempt_binding = (
            RunEventCancellationBinding(
                AgentAttemptId(_digest(f"attempt-{run_id.value}")),
                1,
                AgentAttemptReplacement.NONE,
                "cancel-1",
                (
                    AgentAttemptCancellationDisposition.NEVER_LAUNCHED
                    if event_kind
                    in {
                        RunEventKind.AGENT_CANCELLED,
                        RunEventKind.AGENT_INTERRUPTED,
                    }
                    else None
                ),
            )
            if cancellation
            else None
        )
        event = RunEvent(
            run_id,
            WorkflowRevisionHash(revision.revision_hash.value),
            sequence,
            node_id,
            NodeExecutionId.for_node(
                run_id, WorkflowRevisionHash(revision.revision_hash.value), node_id
            ),
            event_kind,
            payload,
            attempt_binding=attempt_binding,
        )
        event_records.append(
            {
                "run_id": event.run_id.value,
                "revision_hash": event.revision_hash.value,
                "event_sequence": event.event_sequence,
                "node_id": event.node_id,
                "node_execution_id": event.node_execution_id.value,
                "round_ordinal": event.round_ordinal,
                "event_kind": event.event_kind.value,
                "payload": event.payload,
                "payload_hash": event.payload_hash.value,
                "receipt_logical_key": None,
                "receipt_result_hash": None,
                "event_hash": event.event_hash.value,
                "agent_attempt_id": (
                    None
                    if attempt_binding is None
                    else attempt_binding.attempt_id.value
                ),
                "attempt_ordinal": (
                    None if attempt_binding is None else attempt_binding.attempt_ordinal
                ),
                "cancellation_command_id": (
                    None if attempt_binding is None else attempt_binding.command_id
                ),
                "replacement": (
                    None
                    if attempt_binding is None
                    else attempt_binding.replacement.value
                ),
                "cancellation_disposition": (
                    None
                    if attempt_binding is None or attempt_binding.disposition is None
                    else attempt_binding.disposition.value
                ),
                "replacement_attempt_id": (
                    None
                    if attempt_binding is None
                    or attempt_binding.replacement_attempt_id is None
                    else attempt_binding.replacement_attempt_id.value
                ),
            }
        )
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
        configuration_hash: str | None = None
        if workflow_format_version == 3:
            # The schema keeps a V3 row honest: it stands on the configuration
            # revision it was started under, and the preimage that hash names.
            configuration_hash = _digest(f"configuration-{run_id.value}")
            connection.execute(
                run_configuration_revisions.insert().values(
                    revision_hash=configuration_hash,
                    preimage=b"seeded run configuration",
                )
            )
        connection.execute(
            runs.insert().values(
                run_id=run_id.value,
                bootstrap_workflow_id=f"workflow-{run_id.value}",
                revision_hash=revision.revision_hash.value,
                workflow_format_version=workflow_format_version,
                run_configuration_revision_hash=configuration_hash,
                agent_binding_set_hash=None,
                current_node_id=(
                    sink_node_id
                    if sink_node_id is not None
                    else ("final" if state is RunState.COMPLETED else "agent")
                ),
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state=state.value,
                state_version=head,
                last_event_sequence=head,
                terminal_hash=_digest("terminal")
                if state in {RunState.COMPLETED, RunState.FAILED}
                else None,
            )
        )
        if event_records:
            connection.execute(run_events.insert(), event_records)
    return revision


def test_projection_document_limit_refuses_before_workflow_parse(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = WorkflowRevision(_workflow_document())
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )

    def unexpected_parse(_document: bytes) -> object:
        raise AssertionError("oversized durable document reached the YAML parser")

    monkeypatch.setattr(queries_module, "parse_workflow_document", unexpected_parse)
    result = durable_queries(
        engine,
        WorkflowPublicationLimits(
            maximum_document_bytes=len(revision.document) - 1,
            maximum_nodes=10,
            maximum_string_characters=100,
            maximum_payload_bytes=100,
        ),
    ).get_workflow_revision(revision.revision_hash)

    assert result == ProjectionTooLarge()


def _store_workflow_revisions(
    engine: Engine, revisions: tuple[WorkflowRevision, ...]
) -> None:
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert(),
            tuple(
                {
                    "revision_hash": revision.revision_hash.value,
                    "document": revision.document,
                }
                for revision in revisions
            ),
        )


def test_workflow_revision_parse_is_reused_across_readers(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("parse-cache-shared"))
    _store_workflow_revisions(engine, (revision,))
    original_parse = queries_module.parse_workflow_document
    parse_count = 0

    def count_parse(document: bytes) -> object:
        nonlocal parse_count
        parse_count += 1
        return original_parse(document)

    monkeypatch.setattr(queries_module, "parse_workflow_document", count_parse)

    first = durable_queries(engine).get_workflow_revision(revision.revision_hash)
    second = durable_queries(engine).get_workflow_revision(revision.revision_hash)

    assert isinstance(first, WorkflowRevisionFound)
    assert second == first
    assert parse_count == 1


def test_each_workflow_revision_hash_is_parsed_once(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revisions = (
        WorkflowRevision(_v3_workflow_document("parse-cache-first")),
        WorkflowRevision(_v3_workflow_document("parse-cache-second")),
    )
    _store_workflow_revisions(engine, revisions)
    original_parse = queries_module.parse_workflow_document
    parse_counts = {revision.document: 0 for revision in revisions}

    def count_parse(document: bytes) -> object:
        parse_counts[document] += 1
        return original_parse(document)

    monkeypatch.setattr(queries_module, "parse_workflow_document", count_parse)
    budget = EnrichedPageBudget(maximum_nodes=100, maximum_document_bytes=100_000)

    first = durable_queries(engine).list_described_workflow_revisions(None, 50, budget)
    second = durable_queries(engine).list_described_workflow_revisions(None, 50, budget)

    assert isinstance(first, DescribedWorkflowRevisionPage)
    assert second == first
    assert parse_counts == {revision.document: 1 for revision in revisions}


def test_workflow_revision_parse_failure_is_retried(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("parse-cache-retry"))
    _store_workflow_revisions(engine, (revision,))
    parse_count = 0

    def fail_parse(_document: bytes) -> object:
        nonlocal parse_count
        parse_count += 1
        raise ValueError("temporary parser failure")

    monkeypatch.setattr(queries_module, "parse_workflow_document", fail_parse)

    first = durable_queries(engine).get_workflow_revision(revision.revision_hash)
    second = durable_queries(engine).get_workflow_revision(revision.revision_hash)

    assert first == QueryDurableStateCorrupt()
    assert second == QueryDurableStateCorrupt()
    assert parse_count == 2


def test_event_payload_limit_refuses_before_event_materialization(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = RunId("oversized-event")
    revision = _seed_history(engine, run_id=run_id, head=10)

    def unexpected_materialization(_record: object) -> object:
        raise AssertionError("oversized durable event reached the event mapper")

    monkeypatch.setattr(queries_module, "event_from_record", unexpected_materialization)
    result = durable_queries(
        engine,
        WorkflowPublicationLimits(
            maximum_document_bytes=len(revision.document),
            maximum_nodes=10,
            maximum_string_characters=100,
            maximum_payload_bytes=1,
        ),
    ).read_run_event_page(run_id, 9, 1)

    assert result == ProjectionTooLarge()


def test_query_connection_restores_pooled_busy_timeout(engine: Engine) -> None:
    with engine.connect() as connection:
        raw_before = connection.connection.driver_connection
        assert isinstance(raw_before, sqlite3.Connection)
        connection.exec_driver_sql("PRAGMA busy_timeout=250")
        raw_identity = id(raw_before)

    result = (
        durable_queries(engine)
        .__class__(engine, permissive_projection_limit(), busy_timeout_seconds=0.001)
        .list_workflow_revisions(None, 1)
    )

    assert isinstance(result, WorkflowRevisionPage)
    with engine.connect() as connection:
        assert id(connection.connection.driver_connection) == raw_identity
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 250
        raw_after = connection.connection.driver_connection
        assert isinstance(raw_after, sqlite3.Connection)
        assert not raw_after.in_transaction


def test_pool_checkout_timeout_is_a_typed_read_unavailable(tmp_path: Path) -> None:
    configured = sa.create_engine(
        f"sqlite:///{tmp_path / 'pool-timeout.sqlite'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=NO_WAIT_FOR_A_POOLED_CONNECTION,
    )
    initialize_schema(configured)
    try:
        with configured.connect():
            result = durable_queries(configured).list_workflow_revisions(None, 1)
        assert isinstance(result, ReadUnavailable)
    finally:
        configured.dispose()


@pytest.mark.parametrize(
    ("busy_timeout_seconds", "query_deadline_seconds"),
    [
        (0.0005, 1.0),
        (float("inf"), 1.0),
        (1.0, float("nan")),
    ],
)
def test_query_timing_rejects_unrepresentable_or_unbounded_values(
    engine: Engine, busy_timeout_seconds: float, query_deadline_seconds: float
) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        DbosQueries(
            engine,
            permissive_projection_limit(),
            busy_timeout_seconds=busy_timeout_seconds,
            query_deadline_seconds=query_deadline_seconds,
        )


def test_restored_timeout_preserves_a_subsequent_contended_write(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=250")
    assert isinstance(
        durable_queries(engine)
        .__class__(engine, permissive_projection_limit(), busy_timeout_seconds=0.001)
        .list_workflow_revisions(None, 1),
        WorkflowRevisionPage,
    )
    database_path = Path(engine.url.database or "")
    lock_owner = sqlite3.connect(database_path, timeout=0, isolation_level=None)
    lock_owner.execute("BEGIN IMMEDIATE")
    revision = WorkflowRevision(_workflow_document("contended"))
    write_started = Event()

    def write_after_query() -> None:
        with engine.begin() as connection:
            write_started.set()
            connection.execute(
                workflow_revisions.insert().values(
                    revision_hash=revision.revision_hash.value,
                    document=revision.document,
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(write_after_query)
            assert write_started.wait(timeout=2)
            time.sleep(0.03)
            lock_owner.commit()
            pending.result(timeout=2)
    finally:
        if lock_owner.in_transaction:
            lock_owner.rollback()
        lock_owner.close()

    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(workflow_revisions.c.document).where(
                    workflow_revisions.c.revision_hash == revision.revision_hash.value
                )
            )
            == revision.document
        )


def test_real_deadline_returns_typed_result_and_clears_progress_handler(
    engine: Engine,
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("deadline"))
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=275")

    def make_revision_read_deliberately_long(
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> tuple[str, object]:
        if "workflow_revisions" not in statement or "counter" in statement:
            return statement, parameters
        # The read is bounded now, so its own columns must survive: the delay is
        # wrapped around the real statement instead of replacing it, or the row
        # comes back without the lengths the bound checks and the deadline is
        # never what the test measured.
        long_statement = (
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 1000000"
            ") SELECT bounded.* FROM (" + statement + ") AS bounded, counter "
            "ORDER BY counter.value DESC LIMIT 1"
        )
        return long_statement, parameters

    event.listen(
        engine,
        "before_cursor_execute",
        make_revision_read_deliberately_long,
        retval=True,
    )
    try:
        result = DbosQueries(
            engine, permissive_projection_limit(), query_deadline_seconds=0.001
        ).get_workflow_revision(revision.revision_hash)
        assert isinstance(result, ReadUnavailable)
    finally:
        event.remove(
            engine,
            "before_cursor_execute",
            make_revision_read_deliberately_long,
        )

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 275
        assert (
            connection.exec_driver_sql(
                "WITH RECURSIVE counter(value) AS ("
                "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 10000"
                ") SELECT max(value) FROM counter"
            ).scalar_one()
            == 10000
        )
    assert isinstance(
        durable_queries(engine).get_workflow_revision(revision.revision_hash),
        WorkflowRevisionFound,
    )


def test_cancelled_real_query_restores_pooled_connection_before_reuse(
    engine: Engine,
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("cancelled"))
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=275")
    started = Event()
    release = Event()

    def block_revision_read(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "workflow_revisions.document" in statement:
            started.set()
            release.wait(timeout=5)

    async def scenario() -> None:
        event.listen(engine, "before_cursor_execute", block_revision_read)
        runner = BoundedQueryRunner(1, admission_timeout_seconds=1)
        try:
            task = asyncio.create_task(
                runner.run(
                    lambda: DbosQueries(
                        engine,
                        permissive_projection_limit(),
                        busy_timeout_seconds=0.001,
                    ).get_workflow_revision(revision.revision_hash)
                )
            )
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runner.abandoned_queries == 1
            release.set()
            deadline = asyncio.get_running_loop().time() + 2
            while (
                runner.abandoned_queries
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)
            assert runner.abandoned_queries == 0
        finally:
            release.set()
            event.remove(engine, "before_cursor_execute", block_revision_read)

    asyncio.run(scenario())
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 275
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        assert not raw.in_transaction
    assert isinstance(
        durable_queries(engine).get_workflow_revision(revision.revision_hash),
        WorkflowRevisionFound,
    )


def test_prepare_query_work_is_independent_of_history_length(tmp_path: Path) -> None:
    progress_calls = []
    for name, event_count in (("short", 5), ("long", 5_000)):
        configured = create_canonical_engine(tmp_path / f"{name}.sqlite")
        initialize_schema(configured)
        try:
            run_id = RunId(name)
            _seed_history(configured, run_id=run_id, head=event_count)
            monotonic = CountingMonotonic()
            result = DbosQueries(
                configured, permissive_projection_limit(), monotonic=monotonic
            ).prepare_run_event_stream(run_id, 2)
            assert result == StreamReady(event_count, False, 2)
            progress_calls.append(monotonic.calls)
        finally:
            configured.dispose()

    assert progress_calls[1] <= progress_calls[0] + 2


def test_prepare_uses_bounded_endpoints_and_page_read_detects_middle_gap(
    engine: Engine,
) -> None:
    run_id = RunId("middle-gap")
    _seed_history(
        engine,
        run_id=run_id,
        head=5,
        missing_sequences=frozenset({3}),
    )
    queries = durable_queries(engine)

    assert queries.prepare_run_event_stream(run_id, 0) == StreamReady(5, False, 0)
    assert isinstance(queries.read_run_event_page(run_id, 0, 5), EventHistoryCorrupt)


@pytest.mark.parametrize(("query_kind", "initial_head"), [("page", 1), ("prepare", 0)])
def test_event_query_keeps_one_old_snapshot_when_history_appends_between_selects(
    engine: Engine, query_kind: str, initial_head: int
) -> None:
    run_id = RunId(f"snapshot-{query_kind}")
    revision = _seed_history(engine, run_id=run_id, head=initial_head)
    appended = False

    def append_after_head_read(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal appended
        if appended or "runs.state, runs.last_event_sequence" not in statement:
            return
        appended = True
        sequence = initial_head + 1
        payload = str(sequence).encode()
        with engine.begin() as writer:
            writer.execute(
                run_events.insert().values(
                    run_id=run_id.value,
                    revision_hash=revision.revision_hash.value,
                    event_sequence=sequence,
                    node_id=f"node-{sequence}",
                    node_execution_id=_digest(f"execution-{run_id.value}-{sequence}"),
                    round_ordinal=FIRST_ROUND_ORDINAL,
                    event_kind=RunEventKind.AGENT_COMPLETED.value,
                    payload=payload,
                    payload_hash=hashlib.sha256(payload).hexdigest(),
                    receipt_logical_key=None,
                    receipt_result_hash=None,
                    event_hash=_digest(f"event-{run_id.value}-{sequence}"),
                )
            )
            writer.execute(
                runs.update()
                .where(runs.c.run_id == run_id.value)
                .values(
                    state_version=sequence,
                    last_event_sequence=sequence,
                )
            )

    event.listen(engine, "after_cursor_execute", append_after_head_read)
    try:
        if query_kind == "page":
            result = durable_queries(engine).read_run_event_page(run_id, 0, 5)
            assert isinstance(result, queries_module.RunEventPage)
            assert tuple(event.event.event_sequence for event in result.events) == (1,)
        else:
            assert durable_queries(engine).prepare_run_event_stream(
                run_id, 0
            ) == StreamReady(0, False, 0)
    finally:
        event.remove(engine, "after_cursor_execute", append_after_head_read)

    assert appended
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(runs.c.last_event_sequence).where(
                    runs.c.run_id == run_id.value
                )
            )
            == initial_head + 1
        )


@pytest.mark.parametrize("missing_sequence", [1, 5])
def test_prepare_rejects_missing_history_endpoint_before_stream_headers(
    engine: Engine, missing_sequence: int
) -> None:
    run_id = RunId(f"missing-{missing_sequence}")
    _seed_history(
        engine,
        run_id=run_id,
        head=5,
        missing_sequences=frozenset({missing_sequence}),
    )

    assert isinstance(
        durable_queries(engine).prepare_run_event_stream(run_id, 0),
        EventHistoryCorrupt,
    )


def test_prepare_rejects_a_terminal_v3_run_whose_head_event_left_the_sink(
    engine: Engine,
) -> None:
    """The V3 spelling of an ending is checked as strictly as the V1 one.

    A V3 line ends on its agent sink, and every agent node completes with the
    same kind -- so the node is what says which event ended the run. A completed
    run whose last event stands somewhere else is a torn history, and naming it
    is the whole reason this pre-flight exists (#90).
    """
    run_id = RunId("v3-head-off-the-sink")
    _seed_history(
        engine,
        run_id=run_id,
        head=5,
        state=RunState.COMPLETED,
        workflow_format_version=3,
        sink_node_id=None,
    )

    assert isinstance(
        durable_queries(engine).prepare_run_event_stream(run_id, 0),
        EventHistoryCorrupt,
    )


def test_prepare_opens_a_terminal_v3_run_that_ended_on_its_sink(
    engine: Engine,
) -> None:
    """And the honest history opens, instead of being reported as corruption.

    Before this, every finished V3 run answered `EventHistoryCorrupt` -- the
    loudest thing this system can say, about a store that is intact -- because
    the pre-flight knew only the subworkflow spelling of an ending.
    """
    run_id = RunId("v3-head-on-the-sink")
    _seed_history(
        engine,
        run_id=run_id,
        head=5,
        state=RunState.COMPLETED,
        workflow_format_version=3,
        sink_node_id="sink",
    )

    assert durable_queries(engine).prepare_run_event_stream(run_id, 0) == StreamReady(
        5, True, 0
    )


def test_prepare_rejects_terminal_run_without_terminal_head_event(
    engine: Engine,
) -> None:
    run_id = RunId("terminal-head-mismatch")
    _seed_history(
        engine,
        run_id=run_id,
        head=5,
        state=RunState.COMPLETED,
        terminal_event=False,
    )

    assert isinstance(
        durable_queries(engine).prepare_run_event_stream(run_id, 0),
        EventHistoryCorrupt,
    )


def _seed_runs(
    engine: Engine, assignments: tuple[tuple[RunId, WorkflowRevision], ...]
) -> None:
    """Write the exact run rows a list or page read is measured against.

    The rows are written directly rather than started, because what these tests
    measure is the SQL a page read emits over an exact set of run ids. The
    binding matrix each row stands on is published through its own production
    door, so a seeded run is one the reader accepts rather than one it refuses.
    """

    registry = AgentExecutorRegistry((failing_agent_executor_factory("exact", []),))
    bindings = publish_v3_agent_bindings(engine, registry)
    binding_set = AgentBindingSet(
        tuple(
            AgentBinding(
                AgentRole(binding.role),
                AgentConfigurationRevisionHash(
                    binding.agent_configuration_revision_hash
                ),
            )
            for binding in bindings
        )
    )
    revisions = {assignment.revision_hash: assignment for _, assignment in assignments}
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert(),
            [
                {
                    "revision_hash": revision.revision_hash.value,
                    "document": revision.document,
                }
                for revision in revisions.values()
            ],
        )
        # The schema keeps a V3 row honest: it stands on the configuration
        # revision it was started under, and the preimage that hash names.
        configuration_hashes = {
            run_id.value: _digest(f"configuration-{run_id.value}")
            for run_id, _ in assignments
        }
        connection.execute(
            run_configuration_revisions.insert(),
            [
                {
                    "revision_hash": configuration_hash,
                    "preimage": b"seeded run configuration",
                }
                for configuration_hash in configuration_hashes.values()
            ],
        )
        connection.execute(
            runs.insert(),
            [
                {
                    "run_id": run_id.value,
                    "bootstrap_workflow_id": f"workflow-{run_id.value}",
                    "revision_hash": revision.revision_hash.value,
                    "workflow_format_version": 3,
                    "run_configuration_revision_hash": configuration_hashes[
                        run_id.value
                    ],
                    "agent_binding_set_hash": binding_set.binding_set_hash.value,
                    "current_node_id": "agent",
                    "current_round_ordinal": FIRST_ROUND_ORDINAL,
                    "state": RunState.STARTED.value,
                    "state_version": 0,
                    "last_event_sequence": 0,
                    "terminal_hash": None,
                }
                for run_id, revision in assignments
            ],
        )
        connection.execute(
            run_agent_bindings.insert(),
            [
                {
                    "run_id": run_id.value,
                    "revision_hash": revision.revision_hash.value,
                    "binding_set_hash": binding_set.binding_set_hash.value,
                    "role": binding.role,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash
                    ),
                }
                for run_id, revision in assignments
                for binding in bindings
            ],
        )


@pytest.mark.parametrize(
    ("run_id_value", "current_node_id"),
    [
        ("r" * 10_000, "agent"),
        ("run\0" + "r" * 10_000, "agent"),
        ("run", "n" * 10_000),
        ("run", "agent\0" + "n" * 10_000),
    ],
    ids=(
        "oversized-run-id",
        "nul-suffix-run-id",
        "oversized-current-node",
        "nul-suffix-current-node",
    ),
)
def test_run_core_text_limits_refuse_before_mapper_without_selecting_bootstrap_id(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    run_id_value: str,
    current_node_id: str,
) -> None:
    revision = WorkflowRevision(_workflow_document("bounded-run-row"))
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=run_id_value,
                bootstrap_workflow_id="unused-bootstrap-identity",
                revision_hash=revision.revision_hash.value,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id=current_node_id,
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state=RunState.STARTED.value,
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )

    def unexpected_materialization(_session: object, _records: object) -> object:
        raise AssertionError("oversized durable run text reached the run mapper")

    monkeypatch.setattr(
        queries_module, "runs_from_records_with_bindings", unexpected_materialization
    )
    run_selects: list[str] = []

    def capture_run_select(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "FROM runs" in statement:
            run_selects.append(statement)

    projection_limit = WorkflowPublicationLimits(
        maximum_document_bytes=len(revision.document),
        maximum_nodes=10,
        maximum_string_characters=64,
        maximum_payload_bytes=100,
    )
    event.listen(engine, "before_cursor_execute", capture_run_select)
    try:
        detail = ProjectionTooLarge()
        assert (
            durable_queries(engine, projection_limit).get_run(RunId(run_id_value))
            == detail
        )
        assert durable_queries(engine, projection_limit).list_runs(None, 100) == detail
    finally:
        event.remove(engine, "before_cursor_execute", capture_run_select)

    assert len(run_selects) == 2
    for statement in run_selects:
        selected_columns = statement.partition("FROM runs")[0]
        assert "bootstrap_workflow_id" not in selected_columns
        assert "CAST(runs.run_id AS BLOB)" in selected_columns
        assert "CAST(runs.current_node_id AS BLOB)" in selected_columns


def test_run_pages_follow_exact_utf8_bytes_from_existing_or_missing_boundary(
    engine: Engine,
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("pagination"))
    run_ids = tuple(
        RunId(value)
        for value in ("slash/run", "nul\0run", "Grüße-東京", "alpha", "zeta")
    )
    _seed_runs(engine, tuple((run_id, revision) for run_id in run_ids))
    expected = tuple(sorted(run_ids, key=lambda run_id: run_id.value.encode("utf-8")))
    queries = durable_queries(engine)
    found = []
    after = None
    while True:
        page = queries.list_runs(after, 1)
        assert isinstance(page, RunPage)
        found.extend(projection.run.run_id for projection in healthy_runs(page))
        if page.next_after is None:
            break
        after = page.next_after

    assert tuple(found) == expected
    assert len(found) == len(set(found))
    missing_boundary = RunId("m")
    after_missing = queries.list_runs(missing_boundary, 100)
    assert isinstance(after_missing, RunPage)
    assert tuple(item.run.run_id for item in healthy_runs(after_missing)) == tuple(
        run_id
        for run_id in expected
        if run_id.value.encode("utf-8") > missing_boundary.value.encode("utf-8")
    )
    assert after_missing.next_after is None


def test_list_runs_answers_only_the_named_state(engine: Engine) -> None:
    revision = WorkflowRevision(_v3_workflow_document("state-filter"))
    first = RunId("alpha-run")
    second = RunId("zeta-run")
    _seed_runs(engine, ((first, revision), (second, revision)))

    started = durable_queries(engine).list_runs(None, 100, RunState.STARTED)
    completed = durable_queries(engine).list_runs(None, 100, RunState.COMPLETED)

    assert isinstance(started, RunPage)
    assert {projection.run.run_id for projection in healthy_runs(started)} == {
        first,
        second,
    }
    assert isinstance(completed, RunPage)
    assert completed.runs == ()


def test_the_run_list_route_filters_by_state_and_names_an_unknown_one(
    engine: Engine,
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("state-route"))
    first = RunId("alpha-run")
    second = RunId("zeta-run")
    _seed_runs(engine, ((first, revision), (second, revision)))
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(run_queries=durable_queries(engine)),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    listed = client.get(API_PREFIX + "/runs", params={"state": "STARTED"})
    empty = client.get(API_PREFIX + "/runs", params={"state": "COMPLETED"})
    refused = client.get(API_PREFIX + "/runs", params={"state": "NOT_A_STATE"})

    assert listed.status_code == 200
    assert {item["run"]["run_id"] for item in listed.json()["items"]} == {
        first.value,
        second.value,
    }
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert refused.status_code == 422
    assert refused.json()["type"].endswith("invalid-request")
    assert refused.json()["invalid_fields"] == [
        {
            "path": "query/state",
            "reason": "not a run state this list can filter",
        }
    ]


@pytest.mark.parametrize(
    ("after", "source", "replacement"),
    [
        (None, "ORDER BY runs.run_id", "ORDER BY runs.run_id DESC"),
        (RunId("m"), "runs.run_id > ?", "runs.run_id < ?"),
    ],
)
def test_run_page_refuses_real_rows_when_sqlite_order_or_boundary_is_wrong(
    engine: Engine, after: RunId | None, source: str, replacement: str
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("wrong-order"))
    _seed_runs(
        engine,
        ((RunId("alpha"), revision), (RunId("zeta"), revision)),
    )

    def corrupt_run_order(
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> tuple[str, object]:
        if "FROM runs" in statement and "ORDER BY runs.run_id" in statement:
            return statement.replace(source, replacement), parameters
        return statement, parameters

    event.listen(engine, "before_cursor_execute", corrupt_run_order, retval=True)
    try:
        result = durable_queries(engine).list_runs(after, 100)
    finally:
        event.remove(engine, "before_cursor_execute", corrupt_run_order)

    assert isinstance(result, QueryDurableStateCorrupt)


def test_run_page_query_uses_primary_index_without_scan_or_temp_sort(
    engine: Engine,
) -> None:
    revision = WorkflowRevision(_v3_workflow_document("query-plan"))
    _seed_runs(
        engine,
        tuple((RunId(f"run-{index:03d}"), revision) for index in range(20)),
    )
    captured: list[tuple[str, tuple[Any, ...]]] = []

    def capture_run_page(
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: tuple[Any, ...],
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "FROM runs" in statement and "ORDER BY" in statement:
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_run_page)
    try:
        result = durable_queries(engine).list_runs(RunId("run-005x"), 3)
        assert isinstance(result, RunPage)
    finally:
        event.remove(engine, "before_cursor_execute", capture_run_page)

    assert len(captured) == 1
    statement, parameters = captured[0]
    # Every read is bounded now, so the question is no longer whether the bound's
    # expressions appear — they always do — but whether they cost the index. This
    # asserts both halves: the projection really is bounded, and the plan is still
    # an index search.
    assert "CAST" in statement.upper()
    with engine.connect() as connection:
        plan = tuple(
            str(record[-1]).upper()
            for record in connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + statement, parameters
            )
        )
    assert any("SEARCH RUNS USING INDEX" in detail for detail in plan)
    assert all("SCAN" not in detail and "TEMP B-TREE" not in detail for detail in plan)


RUN_PAGE_STATEMENTS = (
    "agent_attempts",
    "run_agent_bindings",
    "run_forks",
    "run_forks",
    "run_inputs_v3",
    "run_instants",
    "runs",
    "workflow_revisions",
)
"""Every statement one run page costs, named so a ninth cannot arrive quietly.

Six of them a page costs whatever format its runs are: the page of `runs`, the
`run_forks` it may be an origin or a successor of, the `workflow_revisions` it
parses once per distinct document, the `run_instants` it is stamped with, and
the `run_inputs_v3` it was started with. The other two are what a format-3 page
reads besides -- the `run_agent_bindings` its roles resolve to, and the
`agent_attempts` its rail names on the node each run stands at.

Sorted, and carrying `run_forks` twice, because that is what makes a repeated
read show up here as a repeated name instead of as a number nobody can argue
with.
"""


def _page_read(engine: Engine) -> tuple[tuple[str, ...], int]:
    """The statements one run-page read costs, and the documents it parsed."""

    parsed = 0
    original_parse = queries_module.parse_workflow_document

    def count_parse(document: bytes) -> object:
        nonlocal parsed
        parsed += 1
        return original_parse(document)

    read_tables: list[str] = []

    def capture(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        named = re.search(r"FROM ([a-zA-Z_0-9]+)", statement)
        read_tables.append(named.group(1) if named is not None else statement)

    queries_module.parse_workflow_document = count_parse
    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = durable_queries(engine).list_runs(None, 100)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        queries_module.parse_workflow_document = original_parse
    assert isinstance(page, RunPage)
    return tuple(sorted(read_tables)), parsed


def test_run_page_costs_the_same_named_statements_however_many_runs_it_lists(
    engine: Engine,
) -> None:
    """A page reads each table once, and reads the same tables at any size.

    Both halves carry weight. Growth with the page is the N+1 this exists to
    catch -- a per-run binding read showed up here as ten `run_agent_bindings`
    against one. The named budget is what stops a ninth statement arriving
    unexplained, because a constancy assertion alone would be as happy at a
    constant eight hundred as at eight.
    """

    first_revision = WorkflowRevision(_v3_workflow_document("first"))
    second_revision = WorkflowRevision(_v3_workflow_document("second"))
    _seed_runs(
        engine,
        tuple((RunId(f"batch-{index:03d}"), first_revision) for index in range(2)),
    )

    two_runs, parsed_one_document = _page_read(engine)

    _seed_runs(
        engine,
        tuple((RunId(f"batch-{index:03d}"), second_revision) for index in range(2, 10)),
    )

    ten_runs, parsed_two_documents = _page_read(engine)

    assert two_runs == ten_runs
    assert ten_runs == RUN_PAGE_STATEMENTS
    assert (parsed_one_document, parsed_two_documents) == (1, 2)


def test_run_page_batches_orders_in_one_query_and_a_run_without_one_answers_empty(
    engine: Engine,
) -> None:
    revision = WorkflowRevision(_v3_workflow_document())
    run_ids = tuple(RunId(f"orders-{index:03d}") for index in range(5))
    _seed_runs(engine, tuple((run_id, revision) for run_id in run_ids))
    ordered_run_id = run_ids[0]
    schema_hash = _digest("orders-schema")
    order_value = b'{"topic":"launch"}'
    with engine.begin() as connection:
        connection.execute(
            run_inputs_v3.insert().values(
                run_id=ordered_run_id.value,
                name="headline",
                schema_revision_hash=schema_hash,
                value=order_value,
                value_hash=hashlib.sha256(order_value).hexdigest(),
            )
        )
    order_selects: list[str] = []

    def capture_order_select(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "FROM run_inputs_v3" in statement:
            order_selects.append(statement)

    event.listen(engine, "before_cursor_execute", capture_order_select)
    try:
        page = durable_queries(engine).list_runs(None, 100)
    finally:
        event.remove(engine, "before_cursor_execute", capture_order_select)

    assert isinstance(page, RunPage)
    assert len(order_selects) == 1
    assert "run_inputs_v3.run_id IN" in order_selects[0]
    projections = {
        projection.run.run_id: projection for projection in healthy_runs(page)
    }
    assert projections[ordered_run_id].orders == (
        RunInput("headline", PublishedRevisionHash(schema_hash), order_value),
    )
    for unordered_run_id in run_ids[1:]:
        assert projections[unordered_run_id].orders == ()


TERMINAL_RESULT_TABLES = (
    "agent_attempt_receipts_v3",
    "agent_attempts",
    "artifacts",
    "node_receipts_v3",
    "run_events",
)
"""Every table the terminal answer/refusal batching reads (#1045 REVISE C1)."""


def _terminal_result_reads(engine: Engine) -> dict[str, int]:
    """How many SELECTs each terminal-result table costs one `list_runs` page."""

    counts: dict[str, int] = dict.fromkeys(TERMINAL_RESULT_TABLES, 0)

    def capture(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        named = re.search(r"FROM ([a-zA-Z_0-9]+)", statement)
        table = named.group(1) if named is not None else None
        if table in counts:
            counts[table] += 1

    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = durable_queries(engine).list_runs(None, 100)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert isinstance(page, RunPage)
    return counts


def _seed_plain_answered_runs(engine: Engine, run_ids: tuple[RunId, ...]) -> None:
    """Ended V3 runs that each wrote a real answer -- the page's common row."""

    revision = WorkflowRevision(_v3_workflow_document(f"plain-{len(run_ids)}"))
    _seed_runs(engine, tuple((run_id, revision) for run_id in run_ids))
    with engine.begin() as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id.in_(tuple(run_id.value for run_id in run_ids)))
            .values(state=RunState.COMPLETED.value, terminal_hash=_digest("plain-end"))
        )
        for run_id in run_ids:
            payload = f'{{"answer":"{run_id.value}"}}'.encode()
            _insert_event(
                connection,
                RunEvent(
                    run_id,
                    revision.revision_hash,
                    1,
                    "agent",
                    NodeExecutionId.for_node(run_id, revision.revision_hash, "agent"),
                    RunEventKind.AGENT_COMPLETED,
                    payload,
                ),
            )


def _seed_receiptless_ended_run(engine: Engine, run_id: RunId) -> None:
    """An ended V3 run whose node names neither a receipt nor an attempt.

    Not a state a settled run reaches in production -- the terminal
    `node-receipt/v3` write and the run's own FAILED transition land in the
    same repair (see `_run_terminal_results`'s own docstring) -- but it is
    the exact shape the batched `agent_attempts` fallback read has to answer
    honestly with `None` rather than a query per row, so it is written
    directly here the same way `_seed_runs` writes every other shape this
    file measures.
    """

    revision = WorkflowRevision(_v3_workflow_document(f"receiptless-{run_id.value}"))
    _seed_runs(engine, ((run_id, revision),))
    with engine.begin() as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(
                state=RunState.FAILED.value,
                terminal_hash=_digest(f"receiptless-end-{run_id.value}"),
            )
        )


def _seed_artifact_bearing_refusals(engine: Engine, run_ids: tuple[RunId, ...]) -> None:
    """Ended V3 runs whose terminal `node-receipt/v3` names a kept artifact.

    Written directly against the tables a real schema refusal populates
    (`context_packages_v3`, `node_execution_requests_v3`, `node_receipts_v3`,
    `artifacts`) rather than through a second independent production run per
    row -- content addressing is the one fact `_run_terminal_results` depends
    on, and every other column here is filler a real run would also carry,
    just never read by that projection.
    """

    revision = WorkflowRevision(
        _v3_workflow_document(f"artifact-refusal-{len(run_ids)}")
    )
    _seed_runs(engine, tuple((run_id, revision) for run_id in run_ids))
    with engine.begin() as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id.in_(tuple(run_id.value for run_id in run_ids)))
            .values(
                state=RunState.FAILED.value,
                terminal_hash=_digest(f"artifact-refusal-end-{len(run_ids)}"),
            )
        )
        for run_id in run_ids:
            configuration_hash = connection.scalar(
                sa.select(runs.c.run_configuration_revision_hash).where(
                    runs.c.run_id == run_id.value
                )
            )
            execution_id = NodeExecutionId.for_node(
                run_id, revision.revision_hash, "agent"
            )
            manifest = f"context-{run_id.value}".encode()
            package_hash = Sha256Hash.of(manifest)
            connection.execute(
                context_packages_v3.insert().values(
                    package_hash=package_hash.value, manifest=manifest
                )
            )
            request_hash = _digest(f"request-{run_id.value}")
            connection.execute(
                node_execution_requests_v3.insert().values(
                    request_hash=request_hash,
                    node_execution_id=execution_id.value,
                    run_configuration_revision_hash=configuration_hash,
                    context_package_hash=package_hash.value,
                    preimage=b"seeded request",
                )
            )
            kept = keep_artifact(
                connection, Artifact(f"refused by {run_id.value}".encode())
            )
            artifact = kept.artifact
            stored_reason = store_node_receipt_reason(
                "output-schema-refused: seeded for #1045 growth",
                PublishedRevisionHash(_digest(f"schema-{run_id.value}")),
                artifact.artifact_hash,
            )
            connection.execute(
                node_receipts_v3.insert().values(
                    node_execution_id=execution_id.value,
                    disposition="failed",
                    reason=stored_reason,
                    request_hash=request_hash,
                    context_package_hash=package_hash.value,
                    receipt_hash=_digest(f"receipt-{run_id.value}"),
                )
            )


_ATTEMPT_RECEIPT_REFUSAL_COLUMNS = (
    "attempt_id",
    "node_execution_id",
    "request_hash",
    "executor_operational_identity",
    "run_id",
    "workflow_revision_hash",
    "node_id",
    "attempt_ordinal",
    "state",
    "state_version",
    "process_phase",
    "process_owner_id",
    "watchdog_generation_id",
    "cancellation_command_id",
    "cancellation_expected_state_version",
    "replacement",
    "redrive_state",
    "cancellation_disposition",
    "cancellation_workflow_id",
    "failure_code",
    "receipt_hash",
    "runner_manifest_id",
    "runner_generation_id",
    "runner_invocation_id",
    "runner_terminal_evidence_hash",
    "runner_evidence_acceptance_phase",
    "transcript_artifact_hash",
)


def _seed_attempt_receipt_refusal(
    engine: Engine, run_id: RunId, *, mismatched_artifact: bool
) -> None:
    """An ended V3 run whose ordinal-one attempt names a real refusal receipt.

    Written directly against `agent_attempts` and `agent_attempt_receipts_v3`
    -- the receiptless fallback's own two tables -- but every identity column
    is the one the real current-attempt projection independently recomputes
    and cross-checks (`_current_attempt_projection`): the request hash and
    attempt id are derived here through the same production functions that
    projection uses, on the same empty orders and node outputs a freshly
    seeded run actually has, rather than a placeholder that only this file's
    own reader would accept. `mismatched_artifact` asks for the corrupt shape
    `_run_terminal_results` now refuses loudly (#1045 REVISE, second delta,
    second round): an `artifact_hash` that disagrees with the receipt's own
    `value_hash`.
    """

    revision = WorkflowRevision(
        _v3_workflow_document(f"attempt-refusal-{run_id.value}")
    )
    _seed_runs(engine, ((run_id, revision),))
    # `_v3_workflow_document` pins `ANY_JSON_SCHEMA` on the node's one output;
    # a real current-attempt projection (triggered below by a live
    # `agent_attempts` row) resolves that pin, so it has to actually be
    # published here -- idempotent, safe beside every other caller of it.
    published = DbosCatalogStore(engine).publish_revision(ANY_JSON_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published

    graph = queries_module.parse_workflow_document(revision.document)
    node = graph.node("agent")
    assert isinstance(node, AgentNodeV3)
    execution_id = NodeExecutionId.for_node(run_id, revision.revision_hash, "agent")
    with engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    binding = run.agent_bindings[0]
    job = compose_agent_node_job_for_attempt(
        node,
        (),
        (),
        target_node_execution_id=execution_id,
        target_attempt_ordinal=1,
        prior_refusal_receipt=None,
    )
    request = AgentExecutionRequestV2(
        execution_id,
        run_id,
        revision.revision_hash,
        "agent",
        binding,
        AgentExecutorOperationalIdentity("exact-operation"),
        job,
    )
    attempt_id = AgentAttemptId.for_execution(execution_id, request.request_hash, 1)

    attempt_values = dict.fromkeys(_ATTEMPT_RECEIPT_REFUSAL_COLUMNS)
    attempt_values.update(
        {
            "attempt_id": attempt_id.value,
            "node_execution_id": execution_id.value,
            "request_hash": request.request_hash.value,
            "executor_operational_identity": "exact-operation",
            "run_id": run_id.value,
            "workflow_revision_hash": revision.revision_hash.value,
            "node_id": "agent",
            "attempt_ordinal": 1,
            "state": "FAILED",
            "state_version": 2,
            "process_phase": "NONE",
            "failure_code": "OUTPUT_SCHEMA_REFUSED",
            "runner_evidence_acceptance_phase": "NONE",
        }
    )
    with engine.begin() as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(
                state=RunState.FAILED.value,
                terminal_hash=_digest(f"attempt-refusal-end-{run_id.value}"),
            )
        )
        connection.execute(agent_attempts.insert().values(**attempt_values))
        kept = keep_artifact(
            connection, Artifact(f"attempt refusal {run_id.value}".encode())
        )
        artifact = kept.artifact
        stored_value_hash = (
            _digest(f"mismatched-{run_id.value}")
            if mismatched_artifact
            else artifact.artifact_hash.value
        )
        connection.execute(
            agent_attempt_receipts_v3.insert().values(
                attempt_id=attempt_id.value,
                reason="output-schema-refused: seeded for #1045 corruption check",
                schema_revision_hash=_digest(f"schema-{run_id.value}"),
                value_hash=stored_value_hash,
                artifact_hash=artifact.artifact_hash.value,
                receipt_hash=_digest(f"receipt-{run_id.value}"),
            )
        )


def test_terminal_results_cost_a_constant_number_of_statements_across_a_mixed_ended_page(
    engine: Engine,
) -> None:
    """#1045 REVISE C1 (second delta, second round): every path an ended
    run's terminal answer or refusal can resolve through -- a real answer, a
    receipted refusal with a kept artifact, and an ended run whose node
    names neither a receipt nor an attempt -- reads its own table once for
    the whole page, not once per row.

    The artifact-bearing refusals grow with the page too (2 vs 5): a
    restored per-refusal `artifacts` read would show up as that count
    growing, not just as the plain/receiptless counts staying put. Growing
    every shape at once and asserting the five counts stay exactly equal
    (`fewer == more`) is what a fixed-count refusal could not catch.
    """

    _seed_artifact_bearing_refusals(
        engine, tuple(RunId(f"artifact-fixed-{index}") for index in range(2))
    )
    _seed_receiptless_ended_run(engine, RunId("receiptless-fixed"))
    _seed_plain_answered_runs(engine, (RunId("plain-fixed-0"), RunId("plain-fixed-1")))

    fewer = _terminal_result_reads(engine)

    _seed_artifact_bearing_refusals(
        engine, tuple(RunId(f"artifact-grown-{index}") for index in range(5))
    )
    _seed_plain_answered_runs(
        engine, tuple(RunId(f"plain-grown-{index}") for index in range(8))
    )
    _seed_receiptless_ended_run(engine, RunId("receiptless-grown"))

    more = _terminal_result_reads(engine)

    assert fewer == more
    assert fewer == {
        "agent_attempt_receipts_v3": 0,
        "agent_attempts": 2,
        "artifacts": 1,
        "node_receipts_v3": 1,
        "run_events": 1,
    }


def _assert_run_page_carries_one_defective_row(result: object, run_id: RunId) -> None:
    """The row a run's own projection failure becomes on a list page (#1042).

    A single run's projection failing no longer fails the whole page: the
    page still answers, and that one row is told apart as
    `DefectiveRunProjection` instead. `get_run` (the single-run read) stays
    the fail-loud `QueryDurableStateCorrupt` these scenarios predate.
    """
    assert isinstance(result, RunPage)
    assert len(result.runs) == 1
    row = result.runs[0]
    assert isinstance(row, DefectiveRunProjection)
    assert row.run_id == run_id
    assert row.problem_code is RunProjectionProblemCode.DURABLE_STATE_CORRUPT


def test_a_node_execution_with_two_answer_bearing_events_is_a_defective_row(
    engine: Engine,
) -> None:
    """#1045 REVISE C1 (second delta): a duplicate answer-bearing event used
    to be caught by `_node_answer`'s own `.one_or_none()`; the batched read
    keeps the same refusal rather than a dict silently keeping the last one
    seen. Two different answer-bearing kinds on one execution is the shape
    that reaches this: the store's own unique index already refuses two rows
    of the *same* kind on one execution.

    A listed page no longer fails whole for this one run's own defect
    (#1042): the row becomes `DefectiveRunProjection` instead.
    """

    revision = WorkflowRevision(_v3_workflow_document())
    run_id = RunId("duplicate-answer")
    _seed_runs(engine, ((run_id, revision),))
    execution_id = NodeExecutionId.for_node(run_id, revision.revision_hash, "agent")
    with engine.begin() as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(state=RunState.COMPLETED.value, terminal_hash=_digest("dup-end"))
        )
        _insert_event(
            connection,
            RunEvent(
                run_id,
                revision.revision_hash,
                1,
                "agent",
                execution_id,
                RunEventKind.AGENT_COMPLETED,
                b'{"answer":"first"}',
            ),
        )
        _insert_event(
            connection,
            RunEvent(
                run_id,
                revision.revision_hash,
                2,
                "agent",
                execution_id,
                RunEventKind.SUBWORKFLOW_COMPLETED,
                b'{"answer":"second"}',
            ),
        )

    result = durable_queries(engine).list_runs(None, 100)

    _assert_run_page_carries_one_defective_row(result, run_id)


def test_an_attempt_receipt_whose_artifact_disagrees_with_its_value_hash_is_a_defective_row(
    engine: Engine,
) -> None:
    """#1045 REVISE (second delta, second round): the batched attempt-receipt
    fallback dropped `load_output_schema_refusal_receipt`'s own check that an
    attempt's named artifact is the same hash its receipt judged
    (`agent_attempt_store.py`). Restored in `_run_terminal_results`: a
    mismatch is the store disagreeing with itself, never a value this
    projection shows.

    A listed page no longer fails whole for this one run's own defect
    (#1042): the row becomes `DefectiveRunProjection` instead.
    """

    run_id = RunId("mismatched-artifact")
    _seed_attempt_receipt_refusal(engine, run_id, mismatched_artifact=True)

    result = durable_queries(engine).list_runs(None, 100)

    _assert_run_page_carries_one_defective_row(result, run_id)


def test_an_attempt_receipt_whose_artifact_matches_its_value_hash_reads_the_refusal(
    engine: Engine,
) -> None:
    """The honest counterpart to the corruption test right above: a genuine
    attempt-receipt refusal, artifact and value hash agreeing, still reads.
    """

    _seed_attempt_receipt_refusal(
        engine, RunId("matched-artifact"), mismatched_artifact=False
    )

    page = durable_queries(engine).list_runs(None, 100)

    assert isinstance(page, RunPage)
    assert len(page.runs) == 1


def test_a_locked_database_during_row_projection_is_read_unavailable_not_defective(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`OperationalError` is a `DatabaseError` subclass (#1109 delta HIGH): if
    `_run_rows`' `except ... DatabaseError` clauses caught it before the
    `OperationalError` re-raise, a locked or unavailable SQLite would turn
    every row into a `DefectiveRunProjection` and the page would still answer
    200 — a lie. Both the batch and the per-row retry re-raise
    `OperationalError` ahead of `DatabaseError` so `list_runs`'s own
    `except (OperationalError, PoolTimeoutError): return ReadUnavailable()`
    keeps owning it.
    """

    revision = WorkflowRevision(_v3_workflow_document())
    _seed_runs(engine, ((RunId("locked-during-projection"), revision),))

    def locked_projections(*_args: object, **_kwargs: object) -> object:
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(DbosQueries, "_run_projections", locked_projections)

    result = durable_queries(engine).list_runs(None, 100)

    assert isinstance(result, ReadUnavailable)


def test_run_page_batches_waiting_reconciliation_and_projects_command_owner_state(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = WorkflowRevision(
        b"""format_version: 3
name: Waiting reconciliation batch
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: Request the effect this run reconciles.
    outputs:
      - name: request
        schema: {ref: request-schema, revision: schema-request}
  - id: action
    type: action
    operation: {ref: open-pr, revision: schema-operation}
    depends_on: [agent]
    inputs:
      - name: body
        from: {node: agent, output: request}
"""
    )
    waiting_run_id = RunId("waiting")
    owned_run_id = RunId("reconciling")
    logical_keys = {
        run_id: logical_effect_key_for(
            NodeExecutionId.for_node(run_id, revision.revision_hash, "action")
        ).value
        for run_id in (waiting_run_id, owned_run_id)
    }
    request = b"request"
    registry = AgentExecutorRegistry((failing_agent_executor_factory("exact", []),))
    bindings = publish_v3_agent_bindings(engine, registry)
    binding_set = AgentBindingSet(
        tuple(
            AgentBinding(
                AgentRole(binding.role),
                AgentConfigurationRevisionHash(
                    binding.agent_configuration_revision_hash
                ),
            )
            for binding in bindings
        )
    )
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
        connection.execute(
            run_configuration_revisions.insert(),
            [
                {
                    "revision_hash": _digest(f"configuration-{run_id.value}"),
                    "preimage": f"seeded {run_id.value} configuration".encode(),
                }
                for run_id in (waiting_run_id, owned_run_id)
            ],
        )
        connection.execute(
            runs.insert(),
            [
                {
                    "run_id": run_id.value,
                    "bootstrap_workflow_id": f"workflow-{run_id.value}",
                    "revision_hash": revision.revision_hash.value,
                    "workflow_format_version": 3,
                    "agent_binding_set_hash": binding_set.binding_set_hash.value,
                    "run_configuration_revision_hash": _digest(
                        f"configuration-{run_id.value}"
                    ),
                    "current_node_id": "action",
                    "current_round_ordinal": FIRST_ROUND_ORDINAL,
                    "state": RunState.WAITING_RECONCILIATION.value,
                    "state_version": 1,
                    "last_event_sequence": 1,
                    "terminal_hash": None,
                }
                for run_id in (waiting_run_id, owned_run_id)
            ],
        )
        connection.execute(
            run_agent_bindings.insert(),
            [
                {
                    "run_id": run_id.value,
                    "revision_hash": revision.revision_hash.value,
                    "binding_set_hash": binding_set.binding_set_hash.value,
                    "role": binding.role,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash
                    ),
                }
                for run_id in (waiting_run_id, owned_run_id)
                for binding in bindings
            ],
        )
        connection.execute(
            effect_intents.insert(),
            [
                {
                    "logical_key": logical_keys[run_id],
                    "run_id": run_id.value,
                    "canonical_request": request,
                    "request_hash": hashlib.sha256(request).hexdigest(),
                    "workflow_revision_hash": revision.revision_hash.value,
                    "adapter_revision": (
                        "a" * 65 if run_id == waiting_run_id else "adapter-v1"
                    ),
                    "destination_identity": "destination",
                    "adapter_operational_identity": "operation",
                    "operation_name": "open-pr",
                    "state": EffectIntentState.WAITING_RECONCILIATION.value,
                    "state_version": 1,
                    "reconciliation_owner_command_id": None,
                }
                for run_id in (waiting_run_id, owned_run_id)
            ],
        )
        connection.execute(
            reconcile_commands.insert().values(
                command_id="owner-command",
                logical_key=logical_keys[owned_run_id],
                expected_intent_version=1,
                determination="FOUND",
                actor="operator",
                evidence="e" * 65,
                found_effect_id="effect",
                found_result=b"command-result",
                found_result_hash=hashlib.sha256(b"command-result").hexdigest(),
                state=ReconcileCommandState.PENDING.value,
            )
        )
        connection.execute(
            effect_intents.update()
            .where(effect_intents.c.logical_key == logical_keys[owned_run_id])
            .values(
                state=EffectIntentState.RECONCILING.value,
                state_version=2,
                reconciliation_owner_command_id="owner-command",
            )
        )
    selects = 0
    intent_selects: list[str] = []

    def count_selects(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1
        if "FROM effect_intents" in statement:
            intent_selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        page = durable_queries(engine).list_runs(None, 100)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert isinstance(page, RunPage)
    # One more than the eight a format-1 fixture once measured: a V3 run reads
    # its bound roles too, in the one batched `run_agent_bindings` statement
    # `runs_from_records_with_bindings` issues for the whole page rather than
    # per run.
    assert selects == 9
    assert len(intent_selects) == 1
    assert "effect_intents.logical_key IN" in intent_selects[0]
    assert "effect_intents.run_id IN" not in intent_selects[0]
    projections = {
        projection.run.run_id: projection for projection in healthy_runs(page)
    }
    waiting = projections[waiting_run_id].reconciliation
    owned = projections[owned_run_id].reconciliation
    assert waiting is not None
    assert waiting.intent.state is EffectIntentState.WAITING_RECONCILIATION
    assert waiting.pending_command is None
    assert owned is not None
    assert owned.intent.state is EffectIntentState.RECONCILING
    assert owned.pending_command is not None
    assert owned.pending_command.command.command_id.value == "owner-command"
    assert owned.pending_command.state is ReconcileCommandState.PENDING

    def unexpected_materialization(_record: object) -> object:
        raise AssertionError("oversized durable value reached its domain mapper")

    def projection_limit(
        *, maximum_field_characters: int, maximum_payload_bytes: int
    ) -> WorkflowPublicationLimits:
        return WorkflowPublicationLimits(
            maximum_document_bytes=len(revision.document),
            maximum_nodes=10,
            maximum_string_characters=maximum_field_characters,
            maximum_payload_bytes=maximum_payload_bytes,
        )

    cases = (
        (
            "intent_snapshot_from_record",
            waiting_run_id,
            projection_limit(maximum_field_characters=64, maximum_payload_bytes=100),
        ),
        (
            "intent_snapshot_from_record",
            waiting_run_id,
            projection_limit(maximum_field_characters=100, maximum_payload_bytes=6),
        ),
        (
            "command_snapshot_from_record",
            owned_run_id,
            projection_limit(maximum_field_characters=64, maximum_payload_bytes=100),
        ),
        (
            "command_snapshot_from_record",
            owned_run_id,
            projection_limit(maximum_field_characters=100, maximum_payload_bytes=7),
        ),
    )
    for mapper_name, bounded_run_id, limits in cases:
        with monkeypatch.context() as context:
            context.setattr(queries_module, mapper_name, unexpected_materialization)
            assert durable_queries(engine, limits).get_run(bounded_run_id) == (
                ProjectionTooLarge()
            ), mapper_name


@pytest.mark.proves("a-durable-reader-holds-the-bound-it-reads-under")
def test_a_reader_cannot_be_built_without_the_bound_it_reads_under() -> None:
    """There is no unbounded reader to construct any more.

    The bound used to travel as an optional argument on every read, so a caller
    could omit it and a reader would answer without one. It is now what a reader
    is made of, and leaving it out is not a permissive read — it is not a reader.
    """
    with pytest.raises(TypeError):
        DbosQueries(create_engine("sqlite://"))  # type: ignore[call-arg]


@pytest.mark.proves("a-durable-reader-holds-the-bound-it-reads-under")
def test_the_bound_a_reader_holds_is_the_bound_it_applies(engine: Engine) -> None:
    """No caller passes a bound, so the one it was built with has to be the one
    that governs — otherwise the parameter's removal would have quietly removed
    the enforcement with it."""
    revision = WorkflowRevision(_v3_workflow_document("bounded"))
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
    tight = WorkflowPublicationLimits(
        maximum_document_bytes=len(revision.document) - 1,
        maximum_nodes=10,
        maximum_string_characters=100,
        maximum_payload_bytes=100,
    )

    assert (
        durable_queries(engine, tight).get_workflow_revision(revision.revision_hash)
        == ProjectionTooLarge()
    )
    assert isinstance(
        durable_queries(engine).get_workflow_revision(revision.revision_hash),
        WorkflowRevisionFound,
    )


def test_a_finished_v3_page_reads_back_its_events_and_says_the_line_ended(
    engine: Engine,
) -> None:
    """The read the cockpit actually calls, not only the pre-flight before it.

    `prepare_run_event_stream` answering `StreamReady` only buys the 200 and the
    stream headers; the first page is what carries events to the browser. While
    this read knew one spelling of an ending, a finished V3 line left prepare
    happily and then called the store corrupt one call later -- a 200 that
    immediately says `durable-state-corrupt`, which is worse to read than the 500
    it replaced.
    """
    run_id = RunId("v3-page-on-the-sink")
    _seed_history(
        engine,
        run_id=run_id,
        head=3,
        state=RunState.COMPLETED,
        workflow_format_version=3,
        sink_node_id="sink",
    )

    page = durable_queries(engine).read_run_event_page(run_id, 0, 5)

    assert isinstance(page, RunEventPage), page
    assert page.terminal_seen is True
    assert [event.event.event_sequence for event in page.events] == [1, 2, 3]
    assert [event.event.node_id for event in page.events] == [
        "node-1",
        "node-2",
        "sink",
    ]
    assert all(
        event.event.event_kind is RunEventKind.AGENT_COMPLETED for event in page.events
    )


def test_a_v3_page_whose_head_left_the_sink_is_still_corrupt(engine: Engine) -> None:
    """Teaching the read the second spelling does not blunt it.

    A completed V3 run stands on its sink, so a head event somewhere else is a
    torn history -- and naming that is the reason this read checks at all.
    """
    run_id = RunId("v3-page-off-the-sink")
    _seed_history(
        engine,
        run_id=run_id,
        head=3,
        state=RunState.COMPLETED,
        workflow_format_version=3,
        sink_node_id=None,
    )

    assert isinstance(
        durable_queries(engine).read_run_event_page(run_id, 0, 5), EventHistoryCorrupt
    )


def test_a_v1_page_still_refuses_a_finished_run_without_its_subworkflow_ending(
    engine: Engine,
) -> None:
    """The older family keeps the only ending it has ever had."""
    run_id = RunId("v1-page-without-ending")
    _seed_history(engine, run_id=run_id, head=3, state=RunState.COMPLETED)

    assert isinstance(
        durable_queries(engine).read_run_event_page(run_id, 0, 5), EventHistoryCorrupt
    )


def test_a_running_v3_run_whose_head_event_stands_on_its_node_still_reads(
    engine: Engine,
) -> None:
    """An attempt event is not an ending, even where the run still stands.

    Requesting a cancellation writes an event at the node the run occupies and
    advances the run's head without moving it, so for a moment the head event's
    node and the run's node are the same. Asking only "same node?" calls that
    healthy, running history an ended one -- and, against a run that has not
    ended, corruption. The question is whether the event *completed* the node the
    run stands on.
    """
    run_id = RunId("v3-cancel-requested")
    _seed_history(
        engine,
        run_id=run_id,
        head=2,
        state=RunState.STARTED,
        workflow_format_version=3,
        sink_node_id="working",
        head_event_kind=RunEventKind.AGENT_CANCEL_REQUESTED,
    )
    queries = durable_queries(engine)

    assert queries.prepare_run_event_stream(run_id, 0) == StreamReady(2, False, 0)
    page = queries.read_run_event_page(run_id, 0, 5)
    assert isinstance(page, RunEventPage), page
    assert page.terminal_seen is False


@pytest.mark.parametrize(
    "column",
    ("replacement", "cancellation_disposition"),
)
def test_foreign_stored_cancellation_tokens_are_durable_state_corruption(
    engine: Engine,
    column: str,
) -> None:
    run_id, queries = _foreign_cancellation_queries(engine, column)

    result = queries.read_run_event_page(run_id, 0, 5)

    assert isinstance(result, QueryDurableStateCorrupt)


@pytest.mark.parametrize(
    "shape",
    (
        "attempt-one",
        "attempt-two",
        "cancel-requested",
        "cancelled",
        "cancelled-with-replacement",
    ),
)
def test_dbos_flattens_and_reconstructs_the_closed_attempt_binding(
    engine: Engine,
    shape: str,
) -> None:
    run_id = RunId("attempt-binding-roundtrip")
    revision = _seed_history(
        engine,
        run_id=run_id,
        head=0,
        state=RunState.STARTED,
        workflow_format_version=3,
        sink_node_id="working",
    )
    kind, payload, binding, expected_columns = _attempt_binding_scenario(shape)
    event = RunEvent(
        run_id,
        revision.revision_hash,
        1,
        "working",
        NodeExecutionId.for_node(run_id, revision.revision_hash, "working"),
        kind,
        payload,
        attempt_binding=binding,
    )
    with engine.begin() as connection:
        _insert_event(connection, event)
        record = connection.execute(sa.select(run_events)).mappings().one()

    assert expected_columns == (
        record["agent_attempt_id"],
        record["attempt_ordinal"],
        record["cancellation_command_id"],
        record["replacement"],
        record["cancellation_disposition"],
        record["replacement_attempt_id"],
    )
    assert record["event_hash"] == event.event_hash.value
    assert event_from_record(record) == event


@pytest.mark.parametrize(
    "column",
    ("replacement", "cancellation_disposition"),
)
def test_foreign_stored_cancellation_tokens_refuse_before_api_projection(
    engine: Engine,
    column: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, queries = _foreign_cancellation_queries(engine, column)

    def unexpected_projection(*_arguments: object) -> object:
        raise AssertionError("foreign durable token reached run_event_resource")

    monkeypatch.setattr(stream_module, "run_event_resource", unexpected_projection)

    async def collect_stream() -> list[ServerSentEvent]:
        return [
            frame
            async for frame in stream_server_events(
                PreparedEventStream(
                    run_id,
                    0,
                    1,
                    False,
                    stream_run_projection(run_id.value),
                ),
                stream_page_reader(queries),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(5),
                limits=api_limits(),
                poll_backoff=event_poll_backoff(),
            )
        ]

    frames = asyncio.run(collect_stream())

    assert len(frames) == 1
    assert frames[0].data.problem.type.endswith(":durable-state-corrupt")


def _foreign_cancellation_queries(
    engine: Engine,
    column: str,
) -> tuple[RunId, DbosQueries]:
    run_id = RunId(f"foreign-cancellation-{column}")
    _seed_history(
        engine,
        run_id=run_id,
        head=1,
        state=RunState.STARTED,
        workflow_format_version=3,
        sink_node_id="working",
        head_event_kind=RunEventKind.AGENT_CANCEL_REQUESTED,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.exec_driver_sql("DROP TRIGGER run_events_no_update")
        connection.execute(
            run_events.update()
            .where(run_events.c.run_id == run_id.value)
            .values({column: "FOREIGN_TOKEN"})
        )

    queries = durable_queries(engine)
    return run_id, queries


def _attempt_binding_scenario(
    shape: str,
) -> tuple[
    RunEventKind,
    bytes,
    RunEventAgentAttemptBinding | RunEventCancellationBinding,
    tuple[str, int, str | None, str | None, str | None, str | None],
]:
    attempt_id = AgentAttemptId("b" * 64)
    if shape in {"attempt-one", "attempt-two"}:
        ordinal = 1 if shape == "attempt-one" else 2
        return (
            RunEventKind.AGENT_COMPLETED,
            b"5",
            RunEventAgentAttemptBinding(attempt_id, ordinal),
            (attempt_id.value, ordinal, None, None, None, None),
        )
    replacement_attempt_id = (
        AgentAttemptId("c" * 64) if shape == "cancelled-with-replacement" else None
    )
    disposition = (
        None
        if shape == "cancel-requested"
        else AgentAttemptCancellationDisposition.REAPED_AFTER_TERM
    )
    return (
        (
            RunEventKind.AGENT_CANCEL_REQUESTED
            if disposition is None
            else RunEventKind.AGENT_CANCELLED
        ),
        b"cancel",
        RunEventCancellationBinding(
            attempt_id,
            1,
            AgentAttemptReplacement.ONE,
            "cancel",
            disposition,
            replacement_attempt_id,
        ),
        (
            attempt_id.value,
            1,
            "cancel",
            AgentAttemptReplacement.ONE.value,
            None if disposition is None else disposition.value,
            (None if replacement_attempt_id is None else replacement_attempt_id.value),
        ),
    )


def test_a_v3_page_carries_a_failed_agent_attempt_instead_of_refusing_it(
    engine: Engine,
) -> None:
    """A failure event is V2's shape and V3's too, because they share the arm.

    The projection refused an agent failure outside format 2, from a time when
    only V1 and V2 existed. A V3 attempt fails through the same store, so that
    check called an ordinary failed attempt a corrupt store.
    """
    run_id = RunId("v3-failed-attempt")
    _seed_history(
        engine,
        run_id=run_id,
        head=2,
        state=RunState.FAILED,
        workflow_format_version=3,
        sink_node_id="working",
        head_event_kind=RunEventKind.AGENT_FAILED,
        head_event_payload=(
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY.value.encode("ascii")
        ),
    )

    page = durable_queries(engine).read_run_event_page(run_id, 0, 5)

    assert isinstance(page, RunEventPage), page
    assert page.events[-1].event.event_kind is RunEventKind.AGENT_FAILED
