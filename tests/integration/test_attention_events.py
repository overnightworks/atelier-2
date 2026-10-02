"""Same-instant identity exclusion on the attention page.

`recorded_at` is second-precision RFC 3339. Two WAITING_INPUT rows in one
second is the normal case. Resume is not lexicographic
`(recorded_at, run_id, seq) > cursor`: it continues from instant T with every
identity already emitted at T excluded.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.attention_events import (
    ProjectEvent,
    load_attention_event_page,
)
from atelier2.adapters.dbos.instants import record_event_instant
from atelier2.adapters.dbos.queries import DbosQueries, WaitAnswerProjectionCorrupt
from atelier2.adapters.dbos.run_transitions import RunTransitionConflict
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    initialize_schema,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventKind,
    WaitAnswerActor,
)
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.run_events import (
    AttentionEvent,
    AttentionEventCorrupt,
    AttentionEventPage,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    ProjectionLimitExceeded,
)
from tests.scenarios.api import durable_queries, permissive_projection_limit

INSTANT = RecordedAt("2026-08-19T12:00:00Z")
LATER_SORTING_RUN = RunId("z-wait")
EARLIER_SORTING_RUN = RunId("a-wait")
WAIT_DOCUMENT = b"""format_version: 1
start: pause
nodes:
  - {id: pause, type: wait, prompt: Approve, output: approval, next: null}
"""


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    configured = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(configured)
    try:
        yield configured
    finally:
        configured.dispose()


def _waiting_input(run_id: RunId, revision: WorkflowRevision) -> RunEvent:
    node_id = "pause"
    return RunEvent(
        run_id,
        revision.revision_hash,
        1,
        node_id,
        NodeExecutionId.for_node(run_id, revision.revision_hash, node_id),
        RunEventKind.WAITING_INPUT,
        b"",
        wait_answer_actor=WaitAnswerActor.OPERATOR,
    )


def _insert_run(
    connection: Connection, run_id: RunId, revision: WorkflowRevision
) -> None:
    event = _waiting_input(run_id, revision)
    assert event.wait_answer_actor is not None
    connection.execute(
        runs.insert().values(
            run_id=run_id.value,
            bootstrap_workflow_id=f"workflow-{run_id.value}",
            revision_hash=revision.revision_hash.value,
            workflow_format_version=1,
            agent_binding_set_hash=None,
            current_node_id="pause",
            current_round_ordinal=FIRST_ROUND_ORDINAL,
            state=RunState.WAITING_INPUT.value,
            state_version=1,
            last_event_sequence=1,
            terminal_hash=None,
        )
    )
    connection.execute(
        run_events.insert().values(
            run_id=event.run_id.value,
            revision_hash=event.revision_hash.value,
            event_sequence=event.event_sequence,
            node_id=event.node_id,
            node_execution_id=event.node_execution_id.value,
            round_ordinal=event.round_ordinal,
            event_kind=event.event_kind.value,
            wait_answer_actor=event.wait_answer_actor.value,
            payload=event.payload,
            payload_hash=event.payload_hash.value,
            receipt_logical_key=None,
            receipt_result_hash=None,
            event_hash=event.event_hash.value,
            agent_attempt_id=None,
            attempt_ordinal=None,
            cancellation_command_id=None,
            replacement=None,
        )
    )
    record_event_instant(connection, run_id.value, event.event_sequence, at=INSTANT)


def _run_ids(page: AttentionEventPage) -> tuple[RunId, ...]:
    ids: list[RunId] = []
    for item in page.events:
        match item:
            case AttentionEvent(event=event):
                ids.append(event.event.run_id)
            case AttentionEventCorrupt(run_id=run_id):
                ids.append(run_id)
            case _ as unreachable:
                raise AssertionError(unreachable)
    return tuple(ids)


def test_page_after_a_later_sorting_same_instant_wait_still_returns_the_earlier_id(
    engine: Engine,
) -> None:
    """The later-written run id sorts first; the first emitted cursor must not drop it.

    Write `z-wait` at T, page it, then write `a-wait` at the same T. Lexicographic
    `(recorded_at, run_id, seq) > (T, z-wait, 1)` never sees `a-wait`. Same-instant
    identity exclusion does: T's remaining identities, then later instants.
    """
    revision = WorkflowRevision(WAIT_DOCUMENT)
    queries = durable_queries(engine)
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
        _insert_run(connection, LATER_SORTING_RUN, revision)

    first = queries.read_attention_event_page(None, None, 10, ())
    assert isinstance(first, AttentionEventPage)
    assert _run_ids(first) == (LATER_SORTING_RUN,)
    assert first.events[0].recorded_at == INSTANT

    with engine.begin() as connection:
        _insert_run(connection, EARLIER_SORTING_RUN, revision)
    assert EARLIER_SORTING_RUN.value < LATER_SORTING_RUN.value

    after_first_cursor = queries.read_attention_event_page(LATER_SORTING_RUN, 1, 10, ())
    assert isinstance(after_first_cursor, AttentionEventPage)
    assert _run_ids(after_first_cursor) == (EARLIER_SORTING_RUN,)
    assert after_first_cursor.events[0].recorded_at == INSTANT

    after_both_at_t = queries.read_attention_event_page(
        EARLIER_SORTING_RUN, 1, 10, ((LATER_SORTING_RUN, 1),)
    )
    assert isinstance(after_both_at_t, AttentionEventPage)
    assert _run_ids(after_both_at_t) == ()


CORRUPT_RUN = RunId("corrupt-wait")
BINDING_DISAGREES = RunTransitionConflict("current agent attempt binding disagrees")


def test_limit_one_delivers_the_corrupt_row_then_the_healthy_row(
    engine: Engine,
) -> None:
    """A page of one must name the first corrupt row so resume can leave it."""

    revision = WorkflowRevision(WAIT_DOCUMENT)
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
        _insert_run(connection, LATER_SORTING_RUN, revision)
        _insert_run(connection, CORRUPT_RUN, revision)

    with engine.connect() as connection:
        first = load_attention_event_page(
            connection,
            None,
            None,
            1,
            permissive_projection_limit(),
            _failing_projection(BINDING_DISAGREES),
            (),
        )
        assert isinstance(first, AttentionEventPage)
        assert len(first.events) == 1
        assert isinstance(first.events[0], AttentionEventCorrupt)
        assert first.events[0].run_id == CORRUPT_RUN
        assert first.events[0].event_sequence == 1
        assert first.events[0].recorded_at == INSTANT

        second = load_attention_event_page(
            connection,
            CORRUPT_RUN,
            1,
            1,
            permissive_projection_limit(),
            _failing_projection(BINDING_DISAGREES),
            (),
        )

    assert isinstance(second, AttentionEventPage)
    assert len(second.events) == 1
    assert isinstance(second.events[0], AttentionEvent)
    assert second.events[0].event.event.run_id == LATER_SORTING_RUN


def _failing_projection(failure: Exception) -> ProjectEvent:
    def project_event(
        connection: Connection,
        record: Mapping[Any, Any],
        version: WorkflowFormatVersion,
        projection_limit: DurableProjectionLimit,
    ) -> PersistedRunEvent:
        if str(record["run_id"]) == CORRUPT_RUN.value:
            raise failure
        return DbosQueries._event_projection(
            connection, record, version, projection_limit
        )

    return project_event


def test_an_oversized_projection_refuses_the_page_rather_than_naming_a_row(
    engine: Engine,
) -> None:
    """The admitted-size bound is the read edge's answer, not one run's defect."""

    revision = WorkflowRevision(WAIT_DOCUMENT)
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=revision.document,
            )
        )
        _insert_run(connection, CORRUPT_RUN, revision)
        _insert_run(connection, LATER_SORTING_RUN, revision)

    with engine.connect() as connection, pytest.raises(ProjectionLimitExceeded):
        load_attention_event_page(
            connection,
            None,
            None,
            10,
            permissive_projection_limit(),
            _failing_projection(
                ProjectionLimitExceeded("event payload over the bound")
            ),
            (),
        )


def test_a_wait_answer_invariant_uses_the_durable_corruption_signal_not_a_conflict(
    engine: Engine,
) -> None:
    revision = WorkflowRevision(WAIT_DOCUMENT)
    event = RunEvent(
        CORRUPT_RUN,
        revision.revision_hash,
        2,
        "pause",
        NodeExecutionId.for_node(
            CORRUPT_RUN, revision.revision_hash, "pause", FIRST_ROUND_ORDINAL
        ),
        RunEventKind.WAIT_ANSWERED,
        b"17",
    )
    record = {
        "run_id": event.run_id.value,
        "revision_hash": event.revision_hash.value,
        "event_sequence": event.event_sequence,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "round_ordinal": event.round_ordinal,
        "event_kind": event.event_kind.value,
        "wait_answer_actor": None,
        "payload": event.payload,
        "payload_hash": event.payload_hash.value,
        "receipt_logical_key": None,
        "receipt_result_hash": None,
        "event_hash": event.event_hash.value,
        "agent_attempt_id": None,
        "attempt_ordinal": None,
        "cancellation_command_id": None,
        "replacement": None,
        "cancellation_disposition": None,
        "replacement_attempt_id": None,
        "agent_receipt_hash": None,
    }

    with (
        engine.connect() as connection,
        pytest.raises(WaitAnswerProjectionCorrupt) as raised,
    ):
        DbosQueries._event_projection(
            connection,
            record,
            WorkflowFormatVersion.V3,
            permissive_projection_limit(),
        )

    assert not isinstance(raised.value, RunTransitionConflict)
