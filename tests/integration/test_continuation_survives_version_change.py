"""A continuation accepted by deployment X is recovered exactly once by Y."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import sqlalchemy as sa
from dbos import DBOS, WorkflowHandle

from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    intent_snapshot_from_record,
)
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_events,
    runs,
    wait_answers,
)
from atelier2.adapters.dbos.workflow_ids import (
    answer_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerExistingApplied,
    answer_wait_result,
)
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationExistingApplied,
    reconcile_effect_result,
)
from atelier2.contracts.effects import (
    EffectId,
    EffectIntent,
    EffectIntentStateVersion,
    EffectOutcome,
    EffectResult,
    OperatorFoundEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
    logical_effect_key_for_node,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    NO_AGENT_EXECUTORS,
    complete_v3_agent_node,
    prepare_graph_action,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.runtime import recording_exact_runtime
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

VERSION_X = "continuation-executor-X"
VERSION_Y = "continuation-executor-Y"
RUN = RunId("continuation-survives-version-change")
WAIT_NODE = "approve"
ANSWER = b'"accepted"'
TIMEOUT_SECONDS = 12
POLL_SECONDS = 0.025

WAIT_DOCUMENT = b"""format_version: 3
name: An accepted wait completes
nodes:
  - id: approve
    type: wait
    prompt: Approve this change.
""" + declared_output(ANY_JSON_SCHEMA, "approval")


def wait_runtime(root: Path, version: str) -> DbosRuntime:
    return DbosRuntime(
        canonical_runtime_settings(root, version, agent_scratch_root(root)),
        canonical_loopback_effects(root),
        (),
    )


def effect_runtime(root: Path, version: str) -> DbosRuntime:
    return recording_exact_runtime(
        canonical_runtime_settings(root, version, agent_scratch_root(root)),
        canonical_loopback_effects(root),
        b'"request"',
    )


def status_row(root: Path, workflow_id: str) -> tuple[str, str]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        row = connection.execute(
            "SELECT status, application_version FROM workflow_status "
            "WHERE workflow_uuid=?",
            (workflow_id,),
        ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def wait_for_status_row(
    root: Path, workflow_id: str, expected: tuple[str, str]
) -> None:
    """Poll for DBOS's own bookkeeping, not just the business effect it drove.

    A workflow's body durably writes the run's observable state (here: the run
    reaching `COMPLETED`) as one of its own last steps; DBOS marks the
    envelope `SUCCESS` in a separate write immediately afterward, once the
    function has returned. The two are not one transaction, so a caller that
    waits only for the run's state and then reads `workflow_status` once can
    observe that harmless gap. Poll here the same way `_wait_for_run` does.
    """

    deadline = time.monotonic() + TIMEOUT_SECONDS
    observed = ("", "")
    while time.monotonic() < deadline:
        observed = status_row(root, workflow_id)
        if observed == expected:
            return
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"workflow_status stayed {observed!r}, expected {expected!r}")


def wait_for_marker(process: subprocess.Popen[str], marker: Path) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"staging child exited early:\nstdout={stdout}\nstderr={stderr}"
            )
        time.sleep(POLL_SECONDS)
    process.kill()
    stdout, stderr = process.communicate()
    raise AssertionError(
        f"staging child did not reach its barrier:\nstdout={stdout}\nstderr={stderr}"
    )


def stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=TIMEOUT_SECONDS)


def stage_in_child(
    root: Path, continuation: str, barrier: str
) -> subprocess.Popen[str]:
    marker = root / f"{continuation}-{barrier.lower()}-held"
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__)),
            continuation,
            str(root),
            barrier,
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(Path(__file__).parents[2]),
                    str(Path(__file__).parents[2] / "src"),
                )
            ),
        },
    )
    wait_for_marker(process, marker)
    return process


_EXECUTE_WORKFLOW_BY_ID_TARGET = "dbos._queue.execute_workflow_by_id"


def hold_pending_continuation(
    root: Path, workflow_id: str, marker: Path, launch: Callable[[], None]
) -> None:
    """Hold the queue just after DBOS made its real dequeue state durable.

    `dbos._queue` binds `execute_workflow_by_id` into its own module namespace
    from `dbos._core` at import time; that binding, not the defining module, is
    what the queue's dequeue path calls, so the barrier must patch it there.
    `unittest.mock.patch` takes that dotted path as a string and resolves it
    itself, so the barrier never writes a static attribute expression on the
    private `dbos._queue` module for a linter or type checker to read as a
    claim on public API.
    """

    execute_workflow_by_id: Callable[[DBOS, str, bool, bool], WorkflowHandle[Any]]
    execute_workflow_by_id, _ = mock.patch(
        _EXECUTE_WORKFLOW_BY_ID_TARGET
    ).get_original()

    def held(
        dbos: DBOS,
        candidate_workflow_id: str,
        is_recovery: bool,
        is_dequeue: bool,
    ) -> WorkflowHandle[Any]:
        if candidate_workflow_id != workflow_id:
            return execute_workflow_by_id(
                dbos, candidate_workflow_id, is_recovery, is_dequeue
            )
        if status_row(root, workflow_id)[0] != "PENDING":
            raise AssertionError("DBOS invoked continuation before marking it PENDING")
        marker.touch()
        while True:
            time.sleep(1)

    mock.patch(_EXECUTE_WORKFLOW_BY_ID_TARGET, held).start()
    launch()
    while True:
        time.sleep(1)


def answer_request() -> SubmitWaitAnswerRequest:
    revision = WorkflowRevision(WAIT_DOCUMENT)
    execution = NodeExecutionId.for_node(RUN, revision.revision_hash, WAIT_NODE)
    return SubmitWaitAnswerRequest(
        RUN,
        revision.revision_hash,
        WAIT_NODE,
        execution,
        WaitAnswerActor.OPERATOR,
        ANSWER,
    )


def submit_answer(
    request: SubmitWaitAnswerRequest, answerer: DbosWaitAnswerer
) -> object:
    return answer_wait_result(
        request.run_id,
        request.revision_hash,
        request.node_id,
        request.expected_node_execution_id,
        request.actor,
        request.answer_bytes,
        answerer,
    )


def stage_answer(root: Path, barrier: str, marker: Path) -> None:
    lease = wait_runtime(root, VERSION_X)
    request = answer_request()
    workflow_id = answer_workflow_id_for(request.expected_node_execution_id)
    result = submit_answer(request, DbosWaitAnswerer(lease.engine, VERSION_X))
    assert isinstance(result, AnswerAcceptedPending)
    if barrier == "ENQUEUED":
        assert status_row(root, workflow_id) == ("ENQUEUED", VERSION_X)
        marker.touch()
        while True:
            time.sleep(1)
    hold_pending_continuation(
        root=root,
        workflow_id=workflow_id,
        marker=marker,
        launch=lease.launch,
    )


def duplicate_workflow_status_row(
    engine: sa.Engine, source_workflow_id: str, target_workflow_id: str
) -> None:
    """Clone a real `workflow_status` row under a new id for decoy scenario data.

    DBOS owns that table's full column set; cloning a genuinely-enqueued row
    rather than hand-building one keeps every DBOS-owned field it also wrote
    (executor id, class name, and the rest) internally consistent.
    """

    workflow_status = sa.Table("workflow_status", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        source = dict(
            connection.execute(
                sa.select(workflow_status).where(
                    workflow_status.c.workflow_uuid == source_workflow_id
                )
            )
            .mappings()
            .one()
        )
        source.update(workflow_uuid=target_workflow_id)
        connection.execute(workflow_status.insert().values(**source))


def add_noncurrent_answer_decoy(engine: sa.Engine, real_answer_workflow_id: str) -> str:
    """Scenario data for a stale node's answer; it must not retag current work.

    `DbosWaitAnswerer.submit_result` refuses an answer for any node but the
    run's real current one, so this decoy -- unlike the reconciliation decoy
    below -- has no real door to walk through; it is built directly to prove
    `_retag_stranded_answer`'s own node-identity guard, not the answer door's.
    """

    decoy_node_execution = NodeExecutionId.for_node(
        RUN, WorkflowRevision(WAIT_DOCUMENT).revision_hash, "non-current-node"
    )
    decoy_workflow_id = answer_workflow_id_for(decoy_node_execution)
    duplicate_workflow_status_row(engine, real_answer_workflow_id, decoy_workflow_id)
    with engine.begin() as connection:
        source = dict(
            connection.execute(
                sa.select(wait_answers).where(
                    wait_answers.c.answer_workflow_id == real_answer_workflow_id
                )
            )
            .mappings()
            .one()
        )
        source.update(
            node_id="non-current-node",
            node_execution_id=decoy_node_execution.value,
            answer_workflow_id=decoy_workflow_id,
        )
        connection.execute(wait_answers.insert().values(**source))
    return decoy_workflow_id


def reconciliation_command(intent: EffectIntent, command_id: str) -> ReconcileCommand:
    return ReconcileCommand(
        ReconcileCommandId(command_id),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected the exact destination",
        OperatorFoundEffect(EffectId("retagged-effect"), EffectResult(b"result")),
    )


def add_noncurrent_reconciliation_decoy(
    lease: DbosRuntime, revision: WorkflowRevision, current_intent: EffectIntent
) -> str:
    """Scenario data for a prior node's command; it must not retag current work."""

    command_id = ReconcileCommandId("non-current-reconciliation")
    workflow_id = reconcile_workflow_id_for(command_id)
    decoy_key = logical_effect_key_for_node(
        RUN,
        revision.revision_hash,
        V3_EFFECT_LINE_AGENT_NODE_ID,
    )
    with lease.engine.begin() as connection:
        source = dict(
            connection.execute(
                sa.select(effect_intents).where(
                    effect_intents.c.logical_key
                    == current_intent.binding.logical_key.value
                )
            )
            .mappings()
            .one()
        )
        source.update(
            logical_key=decoy_key.value,
            state="WAITING_RECONCILIATION",
            state_version=1,
            reconciliation_owner_command_id=None,
        )
        connection.execute(effect_intents.insert().values(**source))
    with lease.engine.connect() as connection:
        decoy = intent_snapshot_from_record(
            connection.execute(
                sa.select(effect_intents).where(
                    effect_intents.c.logical_key == decoy_key.value
                )
            )
            .mappings()
            .one()
        ).intent
    accepted = reconcile_effect_result(
        reconciliation_command(decoy, command_id.value),
        DbosEffectReconcileCommander(lease.engine, lease.settings),
    )
    assert isinstance(accepted, ReconciliationAcceptedPending)
    assert status_row(lease.settings.database_path.parent, workflow_id) == (
        "ENQUEUED",
        VERSION_X,
    )
    return workflow_id


def stage_reconciliation(root: Path, barrier: str, marker: Path) -> None:
    lease = effect_runtime(root, VERSION_X)
    try:
        with lease.engine.connect() as connection:
            intent = _current_intent(
                connection, WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
            )
        command = reconciliation_command(intent, "continuation-reconcile")
        accepted = reconcile_effect_result(
            command, DbosEffectReconcileCommander(lease.engine, lease.settings)
        )
        assert isinstance(accepted, ReconciliationAcceptedPending)
        workflow_id = reconcile_workflow_id_for(command.command_id)
        if barrier == "ENQUEUED":
            assert status_row(root, workflow_id) == ("ENQUEUED", VERSION_X)
            marker.touch()
            while True:
                time.sleep(1)
        hold_pending_continuation(root, workflow_id, marker, lease.launch)
    finally:
        lease.close()


def child_main(continuation: str, root: Path, barrier: str, marker: Path) -> None:
    if continuation == "answer":
        stage_answer(root, barrier, marker)
    else:
        stage_reconciliation(root, barrier, marker)


@pytest.mark.parametrize("barrier", ("ENQUEUED", "PENDING"))
def test_an_accepted_answer_continues_once_after_a_version_change(
    tmp_path: Path, barrier: str
) -> None:
    first = wait_runtime(tmp_path, VERSION_X)
    first.initialize_storage()
    try:
        revision = WorkflowRevision(WAIT_DOCUMENT)
        publish_pinned_revisions(first.engine, ANY_JSON_SCHEMA)
        start_published_v3_run(
            first.engine, first.settings, RUN, revision, NO_AGENT_EXECUTORS, roles=()
        )
        first.launch()
        _wait_for_run(first, RunState.WAITING_INPUT)
    finally:
        first.close()
    staged = stage_in_child(tmp_path, "answer", barrier)
    stop_child(staged)
    workflow_id = answer_workflow_id_for(answer_request().expected_node_execution_id)
    decoy_runtime = wait_runtime(tmp_path, VERSION_X)
    try:
        decoy_workflow_id = add_noncurrent_answer_decoy(
            decoy_runtime.engine, workflow_id
        )
    finally:
        decoy_runtime.close()
    second = wait_runtime(tmp_path, VERSION_Y)
    try:
        second.launch()
        _wait_for_run(second, RunState.COMPLETED)
        wait_for_status_row(tmp_path, workflow_id, ("SUCCESS", VERSION_Y))
        decoy_status, decoy_version = status_row(tmp_path, decoy_workflow_id)
        assert decoy_version == VERSION_X
        assert decoy_status in {"ENQUEUED", "PENDING"}
        with second.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                    ),
                    {"workflow_id": workflow_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(run_events)
                    .where(
                        run_events.c.run_id == RUN.value,
                        run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value,
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(wait_answers.c.state).where(
                        wait_answers.c.answer_workflow_id == workflow_id
                    )
                )
                == "APPLIED"
            )
        replay = submit_answer(
            answer_request(), DbosWaitAnswerer(second.engine, VERSION_Y)
        )
        assert isinstance(replay, AnswerExistingApplied)
    finally:
        second.close()


@pytest.mark.parametrize("barrier", ("ENQUEUED", "PENDING"))
def test_an_accepted_reconciliation_continues_once_after_a_version_change(
    tmp_path: Path, barrier: str
) -> None:
    first = effect_runtime(tmp_path, VERSION_X)
    first.initialize_storage()
    try:
        revision = WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
        publish_pinned_revisions(first.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
        start_published_v3_run(
            first.engine, first.settings, RUN, revision, first.agent_executor_registry
        )
        complete_v3_agent_node(
            first,
            RUN,
            V3_EFFECT_LINE_AGENT_NODE_ID,
            V3_EFFECT_LINE_AGENT_JOB,
            b'"request"',
        )
        intent_snapshot = prepare_graph_action(
            first.engine, RUN, revision.revision_hash, first.effect_adapter_binding
        )
        with first.engine.begin() as connection:
            commit_resolution(
                connection,
                intent_snapshot.intent.binding.logical_key.value,
                revision.revision_hash.value,
                {"outcome": EffectOutcome.UNKNOWN.value},
            )
    finally:
        first.close()
    staged = stage_in_child(tmp_path, "reconciliation", barrier)
    stop_child(staged)
    decoy_runtime = effect_runtime(tmp_path, VERSION_X)
    try:
        with decoy_runtime.engine.connect() as connection:
            current_intent = _current_intent(connection, revision)
        decoy_workflow_id = add_noncurrent_reconciliation_decoy(
            decoy_runtime, revision, current_intent
        )
    finally:
        decoy_runtime.close()
    workflow_id = reconcile_workflow_id_for(
        ReconcileCommandId("continuation-reconcile")
    )
    second = effect_runtime(tmp_path, VERSION_Y)
    try:
        second.launch()
        _wait_for_run(second, RunState.WAITING_INPUT, V3_EFFECT_LINE_WAIT_NODE_ID)
        wait_for_status_row(tmp_path, workflow_id, ("SUCCESS", VERSION_Y))
        decoy_status, decoy_version = status_row(tmp_path, decoy_workflow_id)
        assert decoy_version == VERSION_X
        assert decoy_status in {"ENQUEUED", "PENDING"}
        with second.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(reconcile_commands)
                    .where(reconcile_commands.c.command_id == "continuation-reconcile")
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                    ),
                    {"workflow_id": workflow_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(reconcile_commands.c.state).where(
                        reconcile_commands.c.command_id == "continuation-reconcile"
                    )
                )
                == "APPLIED"
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(effect_receipts)
                    .where(
                        effect_receipts.c.reconcile_command_id
                        == "continuation-reconcile"
                    )
                )
                == 1
            )
            current_intent = _current_intent(connection, revision)
        replay = reconcile_effect_result(
            reconciliation_command(current_intent, "continuation-reconcile"),
            DbosEffectReconcileCommander(second.engine, second.settings),
        )
        assert isinstance(replay, ReconciliationExistingApplied)
    finally:
        second.close()


def _current_intent(
    connection: sa.Connection, revision: WorkflowRevision
) -> EffectIntent:
    logical_key = logical_effect_key_for_node(
        RUN,
        revision.revision_hash,
        V3_EFFECT_LINE_ACTION_NODE_ID,
    )
    record = (
        connection.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one()
    )
    return intent_snapshot_from_record(record).intent


def _wait_for_run(
    lease: DbosRuntime, expected: RunState, expected_node_id: str | None = None
) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with lease.engine.connect() as connection:
            record = connection.execute(
                sa.select(runs.c.state, runs.c.current_node_id).where(
                    runs.c.run_id == RUN.value
                )
            ).one()
        if record.state == expected.value and (
            expected_node_id is None or record.current_node_id == expected_node_id
        ):
            return
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"run did not reach {expected.value}")


if __name__ == "__main__":
    child_main(sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
