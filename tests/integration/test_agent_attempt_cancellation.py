from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.names import (
    CANCELLATION_WORKFLOW_NAME,
    REPLACEMENT_WORKFLOW_NAME,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    _insert_event,
    load_run,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    run_events,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow import AgentExecutorMap, reconstruct_agent_attempt
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import (
    RunEvent,
    RunEventCancellationBinding,
    RunEventKind,
)
from atelier2.contracts.run_projections import PublicAgentAttemptState
from atelier2.contracts.runs import RunId
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationCommandConflict,
    AgentAttemptReplacementNotAllowed,
)
from atelier2.ports.run_queries import RunFound
from tests.integration.test_agent_attempts import (
    attempt_request,
    attempt_runtime,
    inspecting_executor,
)
from tests.integration.test_v3_attempt_arm import runtime as _ordered_v3_runtime
from tests.integration.test_v3_attempt_arm import started_string_ordered_v3_attempt
from tests.scenarios.agents import (
    agent_attempt_execution,
    runtime_workspace_owner,
    workspace_files_nobody_opens,
)
from tests.scenarios.api import durable_api_client, durable_queries

ordered_v3_runtime = _ordered_v3_runtime


def test_cancel_commits_before_signal_and_exact_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "cancel/durable"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        prepared = store.prepare(execution)
        command = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "cancel-1",
            prepared.state_version,
            AgentAttemptReplacement.NONE,
        )

        accepted = store.request_cancellation(command)
        retried = store.request_cancellation(command)

        assert isinstance(accepted, AgentAttemptCancellationAccepted)
        assert accepted.attempt.state is AgentAttemptState.CANCEL_REQUESTED
        assert retried == accepted
        conflict = store.request_cancellation(
            CancelAgentAttemptRequest(
                execution.request.run_id,
                execution.attempt_id,
                "cancel-2",
                prepared.state_version,
                AgentAttemptReplacement.NONE,
            )
        )
        assert isinstance(conflict, AgentAttemptCancellationCommandConflict)
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(run_events)
                    .where(
                        run_events.c.event_kind
                        == RunEventKind.AGENT_CANCEL_REQUESTED.value
                    )
                )
                == 1
            )

        terminal = store.attest_cancellation_cleanup(
            command,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            None,
            None,
        )
        assert terminal.attempt.state is AgentAttemptState.CANCELLED
        with runtime.engine.connect() as connection:
            terminal_event = (
                connection.execute(
                    sa.select(run_events).where(
                        run_events.c.event_kind == RunEventKind.AGENT_CANCELLED.value
                    )
                )
                .mappings()
                .one()
            )
        assert (
            terminal_event["agent_attempt_id"],
            terminal_event["attempt_ordinal"],
            terminal_event["cancellation_command_id"],
            terminal_event["replacement"],
            terminal_event["cancellation_disposition"],
            terminal_event["replacement_attempt_id"],
        ) == (
            prepared.attempt_id.value,
            prepared.attempt_ordinal,
            command.command_id,
            command.replacement.value,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED.value,
            None,
        )
        assert (
            store.attest_cancellation_cleanup(
                command,
                AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
                None,
                None,
            )
            == terminal
        )
        with pytest.raises(RunTransitionConflict, match="armed current attempt"):
            store.complete_success(execution, AgentExecutionResult(b"late"))
    finally:
        runtime.close()


def test_cancel_replacement_creates_exactly_ordinal_two_and_never_three(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "cancel/replace"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        prepared = store.prepare(execution)
        command = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "replace-1",
            prepared.state_version,
            AgentAttemptReplacement.ONE,
        )
        assert isinstance(
            store.request_cancellation(command), AgentAttemptCancellationAccepted
        )
        terminal = store.attest_cancellation_cleanup(
            command,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            None,
            None,
        )
        expected = AgentAttemptId.for_execution(
            execution.request.node_execution_id, execution.request.request_hash, 2
        )
        assert terminal.replacement_attempt_id == expected
        with runtime.engine.connect() as connection:
            assert tuple(
                connection.execute(
                    sa.select(agent_attempts.c.attempt_ordinal).order_by(
                        agent_attempts.c.attempt_ordinal
                    )
                ).scalars()
            ) == (1, 2)
            terminal_event = (
                connection.execute(
                    sa.select(run_events).where(
                        run_events.c.event_kind == RunEventKind.AGENT_CANCELLED.value
                    )
                )
                .mappings()
                .one()
            )
        assert (
            terminal_event["agent_attempt_id"],
            terminal_event["attempt_ordinal"],
            terminal_event["cancellation_command_id"],
            terminal_event["replacement"],
            terminal_event["cancellation_disposition"],
            terminal_event["replacement_attempt_id"],
        ) == (
            prepared.attempt_id.value,
            prepared.attempt_ordinal,
            command.command_id,
            command.replacement.value,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED.value,
            expected.value,
        )

        replacement = store.load(expected)
        refused = store.request_cancellation(
            CancelAgentAttemptRequest(
                replacement.run_id,
                replacement.attempt_id,
                "replace-2",
                replacement.state_version,
                AgentAttemptReplacement.ONE,
            )
        )
        assert isinstance(refused, AgentAttemptReplacementNotAllowed)
        runtime.launch()
        replacement = _wait_for_attempt_state(
            store, expected, AgentAttemptState.SUCCEEDED
        )
        assert replacement.attempt_ordinal == 2
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 2
            )
    finally:
        runtime.close()


def test_cancellation_replacement_keeps_its_base_job_and_request_hash(
    ordered_v3_runtime: DbosRuntime,
) -> None:
    run_id = RunId("v3/cancellation-keeps-its-base-job")
    execution = started_string_ordered_v3_attempt(ordered_v3_runtime, run_id)
    store = DbosAgentAttemptStore(
        ordered_v3_runtime.engine,
        ordered_v3_runtime.settings.application_version,
    )
    prepared = store.prepare(execution)
    assert prepared.request_hash == execution.request.request_hash
    command = CancelAgentAttemptRequest(
        run_id,
        execution.attempt_id,
        "replace-current",
        prepared.state_version,
        AgentAttemptReplacement.ONE,
    )
    store.request_cancellation(command)
    terminal = store.attest_cancellation_cleanup(
        command,
        AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
        None,
        None,
    )
    assert terminal.replacement_attempt_id is not None
    replacement = store.load(terminal.replacement_attempt_id)
    executors: AgentExecutorMap = {
        entry.key: (
            None,
            entry.manifest_entry.operational_identity,
            entry.manifest_entry.declared_capabilities,
            entry.manifest_entry.carrier,
        )
        for entry in ordered_v3_runtime.agent_executor_registry.entries
    }

    reconstructed = reconstruct_agent_attempt(
        ordered_v3_runtime.datasource,
        executors,
        ordered_v3_runtime.declared_project,
        replacement,
    ).execution

    assert reconstructed.request.job_bytes == execution.request.job_bytes
    assert reconstructed.request.request_hash == execution.request.request_hash
    assert replacement.request_hash == execution.request.request_hash
    assert replacement.attempt_id == AgentAttemptId.for_execution(
        execution.request.node_execution_id,
        execution.request.request_hash,
        2,
    )
    found = durable_queries(ordered_v3_runtime.engine).get_run(run_id)
    assert isinstance(found, RunFound), found
    assert tuple(
        (attempt.attempt_ordinal, attempt.state)
        for attempt in found.projection.agent_attempts
    ) == (
        (1, PublicAgentAttemptState.CANCELLED),
        (2, PublicAgentAttemptState.PREPARED),
    )

    response = durable_api_client(ordered_v3_runtime).get(
        API_PREFIX + "/runs/" + encode_public_run_reference(run_id)
    )

    assert response.status_code == 200, response.text
    rail = response.json()["node_rail"]
    assert rail[0]["attempt"] == {
        "ordinal": 2,
        "state": PublicAgentAttemptState.PREPARED.value,
    }


def test_durable_cancellation_workflow_reaps_the_exact_running_process(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "cancel/running-process")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        executor = inspecting_executor(runtime, delay_seconds=60)
        failures: list[RuntimeError] = []

        def run_attempt() -> None:
            try:
                execute_agent_attempt(
                    execution,
                    executor,
                    store,
                    runtime.agent_process_supervisor,
                    runtime_workspace_owner(runtime),
                    permissions=GRANTS_NOTHING,
                    workspace_files=workspace_files_nobody_opens,
                )
            except RuntimeError as error:
                failures.append(error)

        worker = threading.Thread(target=run_attempt)
        worker.start()
        _wait_for_process_phase(
            store,
            execution.attempt_id,
            AgentAttemptProcessPhase.PROCESS_OBSERVED,
        )
        current = store.load(execution.attempt_id)
        result = store.request_cancellation(
            CancelAgentAttemptRequest(
                current.run_id,
                current.attempt_id,
                "cancel-running",
                current.state_version,
                AgentAttemptReplacement.NONE,
            )
        )

        assert isinstance(result, AgentAttemptCancellationAccepted)
        runtime.launch()
        terminal = _wait_for_attempt_state(
            store, execution.attempt_id, AgentAttemptState.CANCELLED
        )
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert terminal.cancellation is not None
        assert terminal.cancellation.disposition in {
            AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
            AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
        }
        assert len(failures) == 1
        assert isinstance(failures[0], RunTransitionConflict)
        assert len(executor.released_commands) == 1
    finally:
        runtime.close()


def test_durable_cancellation_before_watchdog_attests_never_launched(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "cancel/never-launched")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        prepared = store.prepare(execution)
        accepted = store.request_cancellation(
            CancelAgentAttemptRequest(
                prepared.run_id,
                prepared.attempt_id,
                "cancel-before-watchdog",
                prepared.state_version,
                AgentAttemptReplacement.NONE,
            )
        )
        assert isinstance(accepted, AgentAttemptCancellationAccepted)

        runtime.launch()
        terminal = _wait_for_attempt_state(
            store, execution.attempt_id, AgentAttemptState.CANCELLED
        )

        assert terminal.cancellation is not None
        assert (
            terminal.cancellation.disposition
            is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
        )
        assert terminal.process_owner_id is None
        assert terminal.watchdog_generation_id is None
    finally:
        runtime.close()


def test_the_attempt_path_writes_the_event_row_the_run_store_writer_writes(
    tmp_path: Path,
) -> None:
    through_the_attempt_path = _event_row_from_the_attempt_path(tmp_path / "attempt")
    through_the_run_store_writer = _event_row_from_the_run_store_writer(
        tmp_path / "run-store"
    )

    assert through_the_attempt_path == through_the_run_store_writer
    AgentAttemptId(str(through_the_attempt_path["agent_attempt_id"]))
    assert (
        through_the_attempt_path["attempt_ordinal"],
        through_the_attempt_path["cancellation_command_id"],
        through_the_attempt_path["replacement"],
        through_the_attempt_path["cancellation_disposition"],
        through_the_attempt_path["replacement_attempt_id"],
    ) == (
        1,
        _PARITY_COMMAND_ID,
        AgentAttemptReplacement.NONE.value,
        None,
        None,
    )


def test_cancellation_and_replacement_enqueue_into_a_registered_queue(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "queue/named"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        prepared = store.prepare(execution)
        command = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "queue-1",
            prepared.state_version,
            AgentAttemptReplacement.ONE,
        )
        assert isinstance(
            store.request_cancellation(command), AgentAttemptCancellationAccepted
        )
        store.attest_cancellation_cleanup(
            command, AgentAttemptCancellationDisposition.NEVER_LAUNCHED, None, None
        )

        with runtime.engine.connect() as connection:
            enqueued = {
                str(workflow_name): str(queue_name)
                for workflow_name, queue_name in connection.execute(
                    sa.text("SELECT name, queue_name FROM workflow_status")
                ).all()
            }
            registered = {
                str(queue_name)
                for queue_name in connection.execute(
                    sa.text("SELECT name FROM queues")
                ).scalars()
            }

        assert enqueued.keys() >= {
            CANCELLATION_WORKFLOW_NAME,
            REPLACEMENT_WORKFLOW_NAME,
        }
        assert {
            enqueued[CANCELLATION_WORKFLOW_NAME],
            enqueued[REPLACEMENT_WORKFLOW_NAME],
        } <= registered
    finally:
        runtime.close()


_PARITY_RUN_NAME = "event/row-parity"
_PARITY_COMMAND_ID = "parity-1"


def _event_row_from_the_attempt_path(root: Path) -> Mapping[str, Any]:
    root.mkdir()
    runtime = attempt_runtime(root)
    runtime.initialize_storage()
    try:
        store, prepared = _prepared_parity_attempt(runtime)
        assert isinstance(
            store.request_cancellation(
                CancelAgentAttemptRequest(
                    prepared.run_id,
                    prepared.attempt_id,
                    _PARITY_COMMAND_ID,
                    prepared.state_version,
                    AgentAttemptReplacement.NONE,
                )
            ),
            AgentAttemptCancellationAccepted,
        )
        return _only_event_row(runtime)
    finally:
        runtime.close()


def _event_row_from_the_run_store_writer(root: Path) -> Mapping[str, Any]:
    root.mkdir()
    runtime = attempt_runtime(root)
    runtime.initialize_storage()
    try:
        _, prepared = _prepared_parity_attempt(runtime)
        with canonical_write_transaction(runtime.engine) as connection:
            run = load_run(connection, prepared.run_id)
            _insert_event(
                connection,
                RunEvent(
                    prepared.run_id,
                    prepared.workflow_revision_hash,
                    run.last_event_sequence + 1,
                    prepared.node_id,
                    prepared.node_execution_id,
                    RunEventKind.AGENT_CANCEL_REQUESTED,
                    _PARITY_COMMAND_ID.encode("utf-8"),
                    attempt_binding=RunEventCancellationBinding(
                        prepared.attempt_id,
                        prepared.attempt_ordinal,
                        AgentAttemptReplacement.NONE,
                        _PARITY_COMMAND_ID,
                    ),
                ),
            )
        return _only_event_row(runtime)
    finally:
        runtime.close()


def _prepared_parity_attempt(
    runtime: DbosRuntime,
) -> tuple[DbosAgentAttemptStore, AgentAttempt]:
    execution = agent_attempt_execution(attempt_request(runtime, _PARITY_RUN_NAME))
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    return store, store.prepare(execution)


def _only_event_row(runtime: DbosRuntime) -> Mapping[str, Any]:
    with runtime.engine.connect() as connection:
        rows = connection.execute(sa.select(run_events)).mappings().all()
    assert len(rows) == 1
    return dict(rows[0])


def _wait_for_attempt_state(
    store: DbosAgentAttemptStore,
    attempt_id: AgentAttemptId,
    state: AgentAttemptState,
) -> AgentAttempt:
    """Wait for an AgentAttempt to reach ``state``, not a run.

    Stays local rather than sharing tests/scenarios/run_waiting.py: it polls
    the attempt store directly and must additionally retry the transient
    "agent attempt is missing" conflict the store raises before the attempt
    row exists, which the run-state waiters have no equivalent for.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            attempt = store.load(attempt_id)
        except RunTransitionConflict as error:
            if str(error) != "agent attempt is missing":
                raise
            time.sleep(0.01)
            continue
        if attempt.state is state:
            return attempt
        time.sleep(0.01)
    raise AssertionError(f"attempt never reached {state.value}")


def _wait_for_process_phase(
    store: DbosAgentAttemptStore,
    attempt_id: AgentAttemptId,
    phase: AgentAttemptProcessPhase,
) -> AgentAttempt:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            attempt = store.load(attempt_id)
        except RunTransitionConflict as error:
            if str(error) != "agent attempt is missing":
                raise
            time.sleep(0.01)
            continue
        if attempt.process_phase is phase:
            return attempt
        time.sleep(0.01)
    raise AssertionError(f"attempt never reached {phase.value}")
