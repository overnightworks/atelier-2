from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Never

import pytest
import sqlalchemy as sa
from dbos import SQLAlchemyDatasource
from sqlalchemy.engine import Engine

import atelier2.adapters.dbos.runtime as dbos_runtime
from atelier2.adapters.dbos.queue_sweep import QUEUE_SWEEP_THREAD_NAME
from atelier2.adapters.dbos.runtime import (
    AgentProcessSupervisorUnavailable,
    DbosRuntime,
    DbosRuntimeBindingConflict,
    DbosRuntimeLeaseClosed,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    reconcile_commands,
    runs,
)
from atelier2.adapters.dbos.workflow_ids import (
    bootstrap_workflow_id_for,
    effect_workflow_id_for,
    node_workflow_id_for,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    ProviderId,
)
from atelier2.contracts.effects import (
    EFFECT_INTENT_VERSION_CONFIRMED_INITIAL,
    EFFECT_INTENT_VERSION_RECONCILING,
    EFFECT_INTENT_VERSION_WAITING,
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentState,
    EffectReadback,
    EffectUnknownOutcome,
    LogicalEffectKey,
    PerformedEffect,
    ReadbackPhase,
    ReconcileCommandState,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.agent_executions import (
    AgentExecutorFactoryV2,
    AgentExecutorKey,
)
from atelier2.ports.effects import EffectAdapter
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    failing_agent_executor_factory,
)
from tests.scenarios.open_pr_agent import (
    AGENT_NODE_ID,
    PR_SPEC,
    create_open_pr_agent_run,
    open_pr_agent_executor_factory,
    publish_open_pr_agent_run,
)
from tests.scenarios.runs import (
    NO_AGENT_EXECUTORS,
    complete_v3_agent_node,
    prepare_and_launch_graph_action,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.runtime import (
    binding_refusal_of,
    recording_exact_runtime,
    wait_for_sweep,
)
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    V3_EFFECT_LINE_AGENT_JOB,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
    V3_WAIT_LINE_DOCUMENT,
)

WORKFLOW_TIMEOUT_SECONDS = 5.0
WORKFLOW_POLL_SECONDS = 0.025
BARRIER_TIMEOUT_SECONDS = 5.0
WORKFLOW_DOCUMENT = V3_WAIT_LINE_DOCUMENT

AcquireLease = Callable[[DbosRuntimeSettings], DbosRuntime]


class CountingAdapter:
    def __init__(self, delegate: EffectAdapter) -> None:
        self._delegate = delegate
        self.closes = 0

    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        return self._delegate.readback(intent, phase)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        return self._delegate.execute(intent)

    def close(self) -> None:
        self.closes += 1
        self._delegate.close()


class CountingFactory:
    def __init__(self, delegate: LoopbackEffectAdapterFactory) -> None:
        self._delegate = delegate
        self.opens = 0
        self.opened: CountingAdapter | None = None

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

    def open(self) -> CountingAdapter:
        self.opens += 1
        self.opened = CountingAdapter(self._delegate.open())
        return self.opened


def runtime_settings(
    database_path: Path, application_version: str = "executor-A"
) -> DbosRuntimeSettings:
    return DbosRuntimeSettings(database_path, application_version)


def canonical_database(root: Path) -> Path:
    return root / "atelier.sqlite"


def start_wait_run(runtime: DbosRuntime) -> AnyRun:
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA)
    return start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        WorkflowRevision(WORKFLOW_DOCUMENT),
        NO_AGENT_EXECUTORS,
        roles=(),
    )


def run_state(engine: Engine, run_id: RunId) -> RunState:
    with engine.connect() as connection:
        state = connection.scalar(
            sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
        )
    return RunState(str(state))


def wait_until_workflow_succeeds(engine: Engine, workflow_id: str) -> str:
    deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
    status = "PENDING"
    while status != "SUCCESS" and time.monotonic() < deadline:
        with engine.connect() as connection:
            status = str(
                connection.scalar(
                    sa.text(
                        "SELECT status FROM workflow_status WHERE workflow_uuid=:id"
                    ),
                    {"id": workflow_id},
                )
            )
        if status != "SUCCESS":
            time.sleep(WORKFLOW_POLL_SECONDS)
    return status


def wait_until_bootstrap_succeeds(engine: Engine, run_id: RunId) -> str:
    return wait_until_workflow_succeeds(engine, bootstrap_workflow_id_for(run_id))


def wait_until_run_state(engine: Engine, run_id: RunId, expected: RunState) -> RunState:
    deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
    state = run_state(engine, run_id)
    while state is not expected and time.monotonic() < deadline:
        time.sleep(WORKFLOW_POLL_SECONDS)
        state = run_state(engine, run_id)
    return state


def execute_one_bootstrap(runtime: DbosRuntime) -> RunState:
    runtime.initialize_storage()
    started = start_wait_run(runtime)
    runtime.launch()
    assert wait_until_bootstrap_succeeds(runtime.engine, started.run_id) == "SUCCESS"
    return wait_until_run_state(runtime.engine, started.run_id, RunState.WAITING_INPUT)


@pytest.fixture
def acquire() -> Iterator[AcquireLease]:
    leases: list[DbosRuntime] = []

    def acquire_lease(settings: DbosRuntimeSettings) -> DbosRuntime:
        lease = DbosRuntime(
            settings,
            LoopbackEffectAdapterFactory(
                settings.database_path.parent / "external-effect.sqlite",
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )
        leases.append(lease)
        return lease

    yield acquire_lease
    for lease in reversed(leases):
        lease.close()


def test_identical_settings_share_one_process_runtime(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)

    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))

    assert second.engine is first.engine
    assert second.datasource is first.datasource


def test_an_equivalently_spelled_database_path_is_the_same_binding(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))

    second = acquire(runtime_settings(tmp_path / "nested" / ".." / database.name))

    assert second.engine is first.engine


@pytest.mark.parametrize(
    "conflicting",
    [
        pytest.param(
            lambda root: runtime_settings(root / "other.sqlite"), id="other-database"
        ),
        pytest.param(
            lambda root: runtime_settings(canonical_database(root), "executor-B"),
            id="other-application-version",
        ),
    ],
)
def test_an_incompatible_second_binding_is_refused_and_the_active_one_keeps_working(
    acquire: AcquireLease,
    tmp_path: Path,
    conflicting: Callable[[Path], DbosRuntimeSettings],
) -> None:
    active = acquire(runtime_settings(canonical_database(tmp_path)))

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(conflicting(tmp_path))

    assert execute_one_bootstrap(active) is RunState.WAITING_INPUT


def test_a_refused_binding_opens_no_second_canonical_store(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    acquire(runtime_settings(canonical_database(tmp_path)))
    refused = tmp_path / "second" / "atelier.sqlite"

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(refused))

    assert not refused.parent.exists()


def test_closing_one_of_two_identical_leases_keeps_the_executor_running(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.initialize_storage()
    first.launch()

    first.close()

    started = start_wait_run(second)
    assert wait_until_bootstrap_succeeds(second.engine, started.run_id) == "SUCCESS"
    assert (
        wait_until_run_state(second.engine, started.run_id, RunState.WAITING_INPUT)
        is RunState.WAITING_INPUT
    )


def test_a_launched_runtime_sweeps_the_queue_when_asked_and_stops_at_its_close(
    acquire: AcquireLease, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime owns the sweep's clock, from its launch until its close.

    A door that admits an item asks this same runtime for a sweep, so the item
    starts without waiting out the tick; a closed runtime leaves no thread
    sweeping against a binding it no longer holds.
    """

    swept = threading.Event()
    monkeypatch.setattr(
        dbos_runtime, "advance_queue", lambda *_args, **_kwargs: swept.set()
    )
    runtime = acquire(runtime_settings(canonical_database(tmp_path)))
    runtime.initialize_storage()
    runtime.launch()

    wait_for_sweep(runtime, swept)

    runtime.close()
    assert not any(
        thread.name == QUEUE_SWEEP_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_initializing_storage_from_a_second_lease_keeps_the_executor_running(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.initialize_storage()
    first.launch()

    second.initialize_storage()

    started = start_wait_run(first)
    assert wait_until_bootstrap_succeeds(first.engine, started.run_id) == "SUCCESS"
    assert (
        wait_until_run_state(first.engine, started.run_id, RunState.WAITING_INPUT)
        is RunState.WAITING_INPUT
    )


def test_the_last_close_releases_the_binding_for_a_different_one(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))
    first.close()
    second.close()

    rebound = acquire(runtime_settings(tmp_path / "second.sqlite", "executor-B"))

    assert execute_one_bootstrap(rebound) is RunState.WAITING_INPUT


def test_closing_one_lease_twice_does_not_release_the_other(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    database = canonical_database(tmp_path)
    first = acquire(runtime_settings(database))
    second = acquire(runtime_settings(database))

    first.close()
    first.close()

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(tmp_path / "second.sqlite"))
    assert acquire(runtime_settings(database)).engine is second.engine


@pytest.mark.parametrize(
    "use_lease",
    [
        pytest.param(lambda lease: lease.engine, id="engine"),
        pytest.param(lambda lease: lease.datasource, id="datasource"),
        pytest.param(lambda lease: lease.settings, id="settings"),
        pytest.param(lambda lease: lease.launch(), id="launch"),
        pytest.param(lambda lease: lease.initialize_storage(), id="initialize-storage"),
    ],
)
def test_a_closed_lease_refuses_further_use(
    acquire: AcquireLease,
    tmp_path: Path,
    use_lease: Callable[[DbosRuntime], object],
) -> None:
    lease = acquire(runtime_settings(canonical_database(tmp_path)))
    lease.close()

    with pytest.raises(DbosRuntimeLeaseClosed):
        use_lease(lease)


def test_concurrent_closes_of_one_lease_release_it_exactly_once(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    closers = 2
    barrier = Barrier(closers)
    database = canonical_database(tmp_path)
    released = acquire(runtime_settings(database))
    held = acquire(runtime_settings(database))

    def close_together() -> None:
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        released.close()

    with ThreadPoolExecutor(max_workers=closers) as pool:
        for future in [pool.submit(close_together) for _ in range(closers)]:
            future.result()

    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(tmp_path / "second.sqlite"))
    assert execute_one_bootstrap(held) is RunState.WAITING_INPUT


def test_concurrent_identical_acquisitions_hold_one_counted_binding(
    acquire: AcquireLease, tmp_path: Path
) -> None:
    participants = 4
    barrier = Barrier(participants)
    database = canonical_database(tmp_path)

    def acquire_together() -> DbosRuntime:
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        return acquire(runtime_settings(database))

    with ThreadPoolExecutor(max_workers=participants) as pool:
        futures = [pool.submit(acquire_together) for _ in range(participants)]
        leases = [future.result() for future in futures]

    assert all(lease.engine is leases[0].engine for lease in leases)
    for lease in leases[:-1]:
        lease.close()
    with pytest.raises(DbosRuntimeBindingConflict):
        acquire(runtime_settings(tmp_path / "second.sqlite"))
    leases[-1].close()

    rebound = acquire(runtime_settings(tmp_path / "second.sqlite"))
    assert rebound.settings.database_path == tmp_path / "second.sqlite"


def test_equivalent_factories_open_once_and_last_lease_closes_once(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(canonical_database(tmp_path))
    first_factory = CountingFactory(
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )
    second_factory = CountingFactory(first_factory._delegate)
    first = DbosRuntime(settings, first_factory)
    second = DbosRuntime(settings, second_factory)

    assert first_factory.opens == 1
    assert second_factory.opens == 0
    assert first.effect_adapter is second.effect_adapter
    assert first_factory.opened is not None
    first.close()
    assert first_factory.opened.closes == 0
    second.close()
    assert first_factory.opened.closes == 1


def test_incompatible_factory_is_refused_before_it_opens_or_mutates_its_store(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(canonical_database(tmp_path))
    active = DbosRuntime(
        settings,
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    refused_path = tmp_path / "refused" / "external.sqlite"
    refused = CountingFactory(
        LoopbackEffectAdapterFactory(
            refused_path,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )
    try:
        binding_refusal_of(lambda: DbosRuntime(settings, refused))

        assert refused.opens == 0
        assert not refused_path.parent.exists()
    finally:
        active.close()


def test_initialization_failure_closes_the_opened_adapter_and_releases_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = runtime_settings(canonical_database(tmp_path))
    factory = CountingFactory(
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )

    def fail_datasource(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected datasource failure")

    with monkeypatch.context() as context:
        context.setattr(SQLAlchemyDatasource, "create", fail_datasource)
        with pytest.raises(RuntimeError, match="injected datasource failure"):
            DbosRuntime(settings, factory)

    assert factory.opens == 1
    assert factory.opened is not None
    assert factory.opened.closes == 1
    recovered = DbosRuntime(settings, factory._delegate)
    recovered.close()


def _boot_runtime_to_a_confirmed_open_pr_intent(
    settings: DbosRuntimeSettings, factory: LoopbackEffectAdapterFactory
) -> DbosRuntime:
    runtime = recording_exact_runtime(settings, factory, b'"draft-17"')
    runtime.initialize_storage()
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    started = start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        WorkflowRevision(V3_EFFECT_LINE_DOCUMENT),
        runtime.agent_executor_registry,
    )
    runtime.launch()
    assert (
        wait_until_run_state(runtime.engine, started.run_id, RunState.WAITING_INPUT)
        is RunState.WAITING_INPUT
    )
    return runtime


def _prepared_open_pr_intent(
    settings: DbosRuntimeSettings, factory: LoopbackEffectAdapterFactory
) -> tuple[DbosRuntime, EffectIntent]:
    """Boot a runtime to a genuinely PREPARED, never-resolved open-pr intent.

    Drives the same production doors `test_reconcile_effect.py`'s own
    fixtures do: the agent node completes for real through the attempt
    store, and the action's intent is prepared and its `durable_effect`
    workflow enqueued -- but `runtime.launch()` is never called, so nothing
    ever resolves it. A never-confirmed intent needs no forcing to be
    honestly PREPARED: it holds no receipt, and its `durable_effect` row is
    exactly `ENQUEUED`, because `effect_receipts` and `effect_intents` are
    both schema-immutable once a receipt is written -- a PREPARED row cannot
    be forged backward from a confirmed one at all.
    """
    runtime = recording_exact_runtime(settings, factory, b'"draft-17"')
    runtime.initialize_storage()
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    revision = WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
    started = start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        revision,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        started.run_id,
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        b'"draft-17"',
    )
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        started.run_id,
        revision.revision_hash,
        runtime.effect_adapter_binding,
    )
    return runtime, intent


def _abandon_prepared_intent(engine: Engine, logical_key: str) -> None:
    """Write the one CAS the schema allows out of a genuinely fresh PREPARED row."""

    with engine.begin() as connection:
        connection.execute(
            effect_intents.update()
            .where(effect_intents.c.logical_key == logical_key)
            .values(state=EffectIntentState.ABANDONED.value, state_version=1)
        )


_FORCED_INTENT_STATE_VERSION = {
    EffectIntentState.WAITING_RECONCILIATION: EFFECT_INTENT_VERSION_WAITING.value,
    EffectIntentState.RECONCILING: EFFECT_INTENT_VERSION_RECONCILING.value,
    EffectIntentState.CONFIRMED: EFFECT_INTENT_VERSION_CONFIRMED_INITIAL.value,
}
"""The exact `state_version` the real production door leaves at each state
(`atelier2.contracts.effects`), so a forced row cannot masquerade as one no
transition could have written. `PREPARED` and `ABANDONED` are not named here:
neither can be forged from an already-confirmed intent at all, because
`effect_receipts` and the confirmed `effect_intents` row are both
schema-immutable -- see `_prepared_open_pr_intent`."""


def _force_effect_intent_state(engine: Engine, state: EffectIntentState) -> str:
    """Rewrite the sole recorded (already-confirmed) intent's state.

    Only `WAITING_RECONCILIATION`, `RECONCILING`, and `CONFIRMED` are valid
    here (`_FORCED_INTENT_STATE_VERSION` names exactly those); `PREPARED` and
    `ABANDONED` have no honest path back from an already-confirmed row and
    use `_prepared_open_pr_intent` instead. These runtime-boundary tests only
    need a durable row sitting at a given standing; driving the real
    reconciliation store to get there would test machinery this file does
    not own. The version written is the exact one the matching production
    transition leaves (`_FORCED_INTENT_STATE_VERSION`), not whatever this
    fixture's real confirmation happened to leave behind. `RECONCILING` also
    needs its owning command row.
    """
    with engine.begin() as connection:
        logical_key = str(connection.scalar(sa.select(effect_intents.c.logical_key)))
        reconciliation_owner_command_id = None
        if state is EffectIntentState.RECONCILING:
            reconciliation_owner_command_id = f"forced-reconcile-{logical_key}"
            connection.execute(
                reconcile_commands.insert().values(
                    command_id=reconciliation_owner_command_id,
                    logical_key=logical_key,
                    expected_intent_version=0,
                    determination="AUTHORITATIVE_NOT_FOUND",
                    actor="test",
                    evidence="forced for a runtime-boundary test",
                    state=ReconcileCommandState.PENDING.value,
                )
            )
        connection.execute(
            effect_intents.update().values(
                state=state.value,
                state_version=_FORCED_INTENT_STATE_VERSION[state],
                reconciliation_owner_command_id=reconciliation_owner_command_id,
            )
        )
    return logical_key


def _force_run_state(engine: Engine, run_id: RunId, state: RunState) -> None:
    """Rewrite the run's own recorded state, bypassing the workflow that earns it.

    These runtime-boundary tests only need a durable row sitting at a given
    standing; driving the real workflow to get there would test machinery
    this file does not own.
    """
    terminal_hash = (
        Sha256Hash.of(f"test-terminal-{run_id.value}".encode()).value
        if state in TERMINAL_RUN_STATES
        else None
    )
    with engine.begin() as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(state=state.value, terminal_hash=terminal_hash)
        )


def _force_workflow_status(engine: Engine, workflow_id: str, status: str) -> None:
    """Rewrite one DBOS workflow's own bookkeeping row to a chosen status.

    A process crash between a workflow's last commit and its own `SUCCESS`
    has no production door a test can walk through -- proving that boundary
    means reaching into the table DBOS itself owns, exactly like the
    production check this proves (`dbos_runtime._TERMINAL_WORKFLOW_STATUSES`)
    does.

    A workflow commits the run and intent rows an arrangement waits for from
    inside itself, and DBOS records its own terminal status only after it
    returns; waiting for that status first is therefore what keeps this
    rewrite from being overwritten a moment later by the very workflow it
    describes.
    """
    assert wait_until_workflow_succeeds(engine, workflow_id) == "SUCCESS"
    with engine.begin() as connection:
        updated = connection.execute(
            sa.text(
                "UPDATE workflow_status SET status = :status "
                "WHERE workflow_uuid = :workflow_id"
            ),
            {"status": status, "workflow_id": workflow_id},
        )
        assert updated.rowcount == 1


@pytest.mark.parametrize(
    ("intent_state", "run_completes", "confirmed_workflow_status", "expect_refusal"),
    [
        (EffectIntentState.WAITING_RECONCILIATION, False, None, True),
        (EffectIntentState.RECONCILING, False, None, True),
        (EffectIntentState.CONFIRMED, True, "PENDING", True),
        (EffectIntentState.CONFIRMED, True, None, False),
    ],
    ids=[
        "waiting-reconciliation-always-refuses",
        "reconciling-refuses-while-its-command-workflow-is-unresolved",
        "confirmed-refuses-in-the-crash-window-before-its-workflow-succeeds",
        "confirmed-of-a-succeeded-workflow-permits",
    ],
)
def test_restart_binding_conflict_follows_the_owning_workflows_terminal_status(
    tmp_path: Path,
    intent_state: EffectIntentState,
    run_completes: bool,
    confirmed_workflow_status: str | None,
    expect_refusal: bool,
) -> None:
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    original_factory = LoopbackEffectAdapterFactory(
        tmp_path / "external.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )
    runtime = _boot_runtime_to_a_confirmed_open_pr_intent(settings, original_factory)
    logical_key = _force_effect_intent_state(runtime.engine, intent_state)
    if run_completes:
        _force_run_state(runtime.engine, RunId("run-1"), RunState.COMPLETED)
    if confirmed_workflow_status is not None:
        _force_workflow_status(
            runtime.engine,
            effect_workflow_id_for(LogicalEffectKey(logical_key)),
            confirmed_workflow_status,
        )
    runtime.close()
    changed_path = tmp_path / "changed" / "external.sqlite"
    changed_factory = LoopbackEffectAdapterFactory(
        changed_path,
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )

    if expect_refusal:
        failure = binding_refusal_of(
            lambda: recording_exact_runtime(settings, changed_factory, b'"draft-17"')
        )

        message = str(failure)
        assert "open-pr" in message
        assert intent_state.value in message
        assert logical_key[:16] in message
        assert not changed_path.parent.exists()
    else:
        recording_exact_runtime(settings, changed_factory, b'"draft-17"').close()


def test_restart_refuses_a_prepared_intent_whose_workflow_never_finished(
    tmp_path: Path,
) -> None:
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    original_factory = LoopbackEffectAdapterFactory(
        tmp_path / "external.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )
    runtime, intent = _prepared_open_pr_intent(settings, original_factory)
    runtime.close()
    changed_path = tmp_path / "changed" / "external.sqlite"
    changed_factory = LoopbackEffectAdapterFactory(
        changed_path,
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )

    failure = binding_refusal_of(
        lambda: recording_exact_runtime(settings, changed_factory, b'"draft-17"')
    )

    message = str(failure)
    assert "open-pr" in message
    assert EffectIntentState.PREPARED.value in message
    assert intent.binding.logical_key.value[:16] in message
    assert not changed_path.parent.exists()


def test_restart_permits_an_abandoned_intent_of_a_completed_run(
    tmp_path: Path,
) -> None:
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    original_factory = LoopbackEffectAdapterFactory(
        tmp_path / "external.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )
    runtime, intent = _prepared_open_pr_intent(settings, original_factory)
    _abandon_prepared_intent(runtime.engine, intent.binding.logical_key.value)
    _force_run_state(runtime.engine, intent.binding.run_id, RunState.COMPLETED)
    runtime.close()
    changed_path = tmp_path / "changed" / "external.sqlite"
    changed_factory = LoopbackEffectAdapterFactory(
        changed_path,
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )

    recording_exact_runtime(settings, changed_factory, b'"draft-17"').close()


def _duplicate_effect_intent(engine: Engine, copies: int) -> None:
    """Copy the sole recorded intent under distinct logical keys.

    The refusal message's preview bound is a property of how many offending
    rows exist, not of any one row's shape; N differently-keyed copies of an
    already-offending row are the cheapest way to make more than the preview
    limit offend at once. Each copy's own `durable_effect` workflow was never
    minted under its new logical key, so every copy counts on its own --
    unlike the original, whose own owning workflow genuinely succeeded and
    is exempt.
    """
    with engine.begin() as connection:
        original = connection.execute(sa.select(effect_intents)).mappings().one()
        for index in range(copies):
            copy = dict(original)
            copy["logical_key"] = f"{original['logical_key']}-duplicate-{index}"
            connection.execute(effect_intents.insert().values(**copy))


def test_restart_refusal_message_bounds_the_offending_intent_preview(
    tmp_path: Path,
) -> None:
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    original_factory = LoopbackEffectAdapterFactory(
        tmp_path / "external.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )
    runtime = _boot_runtime_to_a_confirmed_open_pr_intent(settings, original_factory)
    preview_limit = dbos_runtime._BINDING_CONFLICT_INTENT_PREVIEW_LIMIT
    omitted_count = 2
    # The original intent's own workflow genuinely succeeded, so only its
    # copies offend; the count is the full total rather than one short of it.
    _duplicate_effect_intent(runtime.engine, preview_limit + omitted_count)
    runtime.close()
    changed_path = tmp_path / "changed" / "external.sqlite"

    failure = binding_refusal_of(
        lambda: recording_exact_runtime(
            settings,
            LoopbackEffectAdapterFactory(
                changed_path,
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
            b'"draft-17"',
        )
    )

    message = str(failure)
    assert message.count("open-pr intent") == preview_limit
    assert f"and {omitted_count} more" in message


def _boot_runtime_to_agent_redeemed_intents(
    settings: DbosRuntimeSettings,
    factory: LoopbackEffectAdapterFactory,
    completed_runs: tuple[RunId, ...],
) -> tuple[DbosRuntime, WorkflowRevisionHash]:
    """Boot a runtime until every named run redeemed its agent node's own grant.

    Drives the production path the live store's own push intents came from: an
    Agent node prepares and redeems what its pinned grant earned as two steps
    of its own node workflow (`workflow.py::redeem_agent_node_effect`), so the
    intent left behind never has a `durable_effect` workflow at all. The grant
    driven here is the `open-pr` one, which this boundary completes without a
    project checkout and its captured candidate; both grant shapes mint the
    key from the same node execution (`logical_effect_key_for_node`), and that
    derivation is the whole of what the ownership question asks.
    """

    runtime = DbosRuntime(settings, factory, (open_pr_agent_executor_factory(PR_SPEC),))
    runtime.initialize_storage()
    workflow, bindings = publish_open_pr_agent_run(runtime, granted=True)
    for run_id in completed_runs:
        create_open_pr_agent_run(runtime, run_id, workflow, bindings)
    runtime.launch()
    for run_id in completed_runs:
        assert (
            wait_until_run_state(runtime.engine, run_id, RunState.COMPLETED)
            is RunState.COMPLETED
        )
    return runtime, workflow.revision_hash


def _agent_redeemed_intent_keys(engine: Engine) -> list[str]:
    """The confirmed intents no `durable_effect` workflow was ever minted for.

    This is what makes an intent the shape #1218 is about, so the arrangement
    is read back rather than assumed: a fixture that quietly produced an
    Action-driven intent would prove nothing about the node-workflow owner.
    """

    with engine.connect() as connection:
        confirmed = [
            str(logical_key)
            for logical_key in connection.scalars(
                sa.select(effect_intents.c.logical_key).where(
                    effect_intents.c.state == EffectIntentState.CONFIRMED.value
                )
            )
        ]
        minted = set(
            connection.scalars(sa.text("SELECT workflow_uuid FROM workflow_status"))
        )
    return [
        logical_key
        for logical_key in confirmed
        if effect_workflow_id_for(LogicalEffectKey(logical_key)) not in minted
    ]


def _restart_under_a_changed_identity(
    settings: DbosRuntimeSettings, changed_path: Path
) -> DbosRuntime:
    return DbosRuntime(
        settings,
        LoopbackEffectAdapterFactory(
            changed_path,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (open_pr_agent_executor_factory(PR_SPEC),),
    )


@pytest.mark.parametrize(
    "completed_runs",
    [1, 5],
    ids=["one-agent-redeemed-intent", "the-live-stores-five-redeemed-intents"],
)
def test_restart_permits_agent_redeemed_intents_of_succeeded_node_workflows(
    tmp_path: Path, completed_runs: int
) -> None:
    """A finished agent-redeemed effect is history, whichever identity opens next.

    Five of them across five completed runs is the live store's own shape on
    05.09.2026, which the first fix read as five forever-open intents and
    refused the moved identity for (#1218).
    """
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    runtime, _ = _boot_runtime_to_agent_redeemed_intents(
        settings,
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        tuple(RunId(f"run-{index}") for index in range(completed_runs)),
    )
    assert len(_agent_redeemed_intent_keys(runtime.engine)) == completed_runs
    runtime.close()

    _restart_under_a_changed_identity(
        settings, tmp_path / "changed" / "external.sqlite"
    ).close()


def test_restart_refuses_an_agent_redeemed_intent_whose_node_workflow_is_unfinished(
    tmp_path: Path,
) -> None:
    """The node workflow that owns the redemption decides, and it has not ended."""
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    run_id = RunId("run-0")
    runtime, revision_hash = _boot_runtime_to_agent_redeemed_intents(
        settings,
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (run_id,),
    )
    logical_key = _agent_redeemed_intent_keys(runtime.engine)[0]
    _force_workflow_status(
        runtime.engine,
        node_workflow_id_for(
            NodeExecutionId.for_node(run_id, revision_hash, AGENT_NODE_ID)
        ),
        "PENDING",
    )
    runtime.close()
    changed_path = tmp_path / "changed" / "external.sqlite"

    failure = binding_refusal_of(
        lambda: _restart_under_a_changed_identity(settings, changed_path)
    )

    message = str(failure)
    assert "open-pr" in message
    assert EffectIntentState.CONFIRMED.value in message
    assert logical_key[:16] in message
    assert not changed_path.parent.exists()


def test_restart_refuses_an_intent_whose_key_no_node_execution_accounts_for(
    tmp_path: Path,
) -> None:
    """Without an owner the intent counts: a store missing its rows fails closed."""
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    runtime, _ = _boot_runtime_to_agent_redeemed_intents(
        settings,
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (RunId("run-0"),),
    )
    _duplicate_effect_intent(runtime.engine, 1)
    runtime.close()
    changed_path = tmp_path / "changed" / "external.sqlite"

    failure = binding_refusal_of(
        lambda: _restart_under_a_changed_identity(settings, changed_path)
    )

    message = str(failure)
    assert message.count("open-pr intent") == 1
    assert not changed_path.parent.exists()


def test_restart_with_the_same_identity_opens_regardless_of_open_intent_state(
    tmp_path: Path,
) -> None:
    settings = DbosRuntimeSettings(
        canonical_database(tmp_path),
        "executor-A",
        agent_scratch_root=agent_scratch_root(tmp_path),
    )
    original_factory = LoopbackEffectAdapterFactory(
        tmp_path / "external.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )
    runtime = _boot_runtime_to_a_confirmed_open_pr_intent(settings, original_factory)
    _force_effect_intent_state(runtime.engine, EffectIntentState.WAITING_RECONCILIATION)
    runtime.close()

    recovered = recording_exact_runtime(settings, original_factory, b'"draft-17"')

    recovered.close()


def test_canonical_and_external_store_must_be_distinct(tmp_path: Path) -> None:
    database = canonical_database(tmp_path)

    failure = binding_refusal_of(
        lambda: DbosRuntime(
            runtime_settings(database),
            LoopbackEffectAdapterFactory(
                database,
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )
    )

    assert "must be distinct" in str(failure)


def test_existing_hardlink_alias_is_refused_before_external_store_mutation(
    tmp_path: Path,
) -> None:
    database = canonical_database(tmp_path)
    original = DbosRuntime(
        runtime_settings(database),
        LoopbackEffectAdapterFactory(
            tmp_path / "original-external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    original.close()
    before = database.read_bytes()
    external_alias = tmp_path / "external-alias.sqlite"
    os.link(database, external_alias)

    failure = binding_refusal_of(
        lambda: DbosRuntime(
            runtime_settings(database),
            LoopbackEffectAdapterFactory(
                external_alias,
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )
    )

    assert "must be distinct" in str(failure)
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'loopback_effect%'"
        ).fetchone() == (0,)


def _runtime_with_v2(
    root: Path,
    factories: tuple[AgentExecutorFactoryV2, ...],
    application_version: str = "v2-life",
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            application_version,
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("lifecycle"),
        ),
        factories,
    )


def test_empty_registry_runs_a_waiting_line_without_process_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_process_authority() -> Never:
        raise AssertionError("empty registry resolved process authority")

    monkeypatch.setattr(
        dbos_runtime, "delegated_cgroup_root", forbidden_process_authority
    )
    monkeypatch.setattr(
        dbos_runtime, "AgentProcessSupervisor", forbidden_process_authority
    )
    runtime = DbosRuntime(
        runtime_settings(canonical_database(tmp_path)),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("lifecycle"),
        ),
    )
    try:
        with pytest.raises(
            AgentProcessSupervisorUnavailable,
            match="no LOCAL_PROCESS-carried executor key",
        ):
            _ = runtime.agent_process_supervisor
        assert execute_one_bootstrap(runtime) is RunState.WAITING_INPUT
    finally:
        runtime.close()

    restarted = DbosRuntime(
        runtime_settings(canonical_database(tmp_path)),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("lifecycle"),
        ),
    )
    try:
        restarted.launch()
        with pytest.raises(
            AgentProcessSupervisorUnavailable,
            match="no LOCAL_PROCESS-carried executor key",
        ):
            _ = restarted.agent_process_supervisor
    finally:
        restarted.close()


def test_nonempty_registry_refuses_missing_cgroup_before_factory_or_store_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude/v1", "operation", b"unused"
    )

    def missing_cgroup() -> Never:
        raise RuntimeError("cgroup root is unavailable")

    monkeypatch.setattr(dbos_runtime, "delegated_cgroup_root", missing_cgroup)

    with pytest.raises(RuntimeError, match="cgroup root is unavailable"):
        _runtime_with_v2(tmp_path, (factory,))

    assert factory.opens == 0
    assert not (tmp_path / "atelier.sqlite").exists()
    assert not (tmp_path / "effects.sqlite").exists()


class ChangingKeyFactory(RecordingAgentExecutorFactoryV2):
    """A factory answering a different key and identity on every single read."""

    @property
    def key(self) -> AgentExecutorKey:
        self.key_reads += 1
        return AgentExecutorKey(
            ProviderId(f"provider-{self.key_reads}"),
            AgentExecutorRevision(f"executor/{self.key_reads}"),
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        self.identity_reads += 1
        return AgentExecutorOperationalIdentity(f"operation-{self.identity_reads}")


def unstable_key_factory() -> ChangingKeyFactory:
    return ChangingKeyFactory("unstable", "unstable/v1", "unstable-operation", b"")


@dataclass
class BaseExceptionEffectFactory:
    binding_value: EffectAdapterBinding
    failure: BaseException
    lifecycle: list[str]

    @property
    def binding(self) -> EffectAdapterBinding:
        return self.binding_value

    @property
    def proves_absence(self) -> bool:
        # This double exists to fail its open, so the runtime never reaches the
        # composed proves_absence read; the value only satisfies the protocol.
        return True

    def open(self) -> Never:
        self.lifecycle.append("open:effect")
        raise self.failure


def test_v2_factories_open_sorted_and_last_lease_closes_them_reverse_once(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    first_factories = (
        RecordingAgentExecutorFactoryV2(
            "openai", "codex/v1", "codex-operation", b"", lifecycle
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "claude-operation", b"", lifecycle
        ),
    )
    second_factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "claude-operation", b""
        ),
        RecordingAgentExecutorFactoryV2("openai", "codex/v1", "codex-operation", b""),
    )

    first = _runtime_with_v2(tmp_path, first_factories)
    second = _runtime_with_v2(tmp_path, second_factories)

    assert lifecycle == ["open:anthropic", "open:openai"]
    assert [factory.opens for factory in first_factories] == [1, 1]
    assert [factory.opens for factory in second_factories] == [0, 0]
    first.close()
    assert lifecycle == ["open:anthropic", "open:openai"]
    second.close()
    assert lifecycle == [
        "open:anthropic",
        "open:openai",
        "close:openai",
        "close:anthropic",
    ]
    assert all(
        factory.opened is not None and factory.opened.closes == 1
        for factory in first_factories
    )


def test_duplicate_v2_registry_key_is_refused_before_any_factory_opens(
    tmp_path: Path,
) -> None:
    factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "first-operation", b""
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "second-operation", b""
        ),
    )

    with pytest.raises(ValueError, match="keys must be unique"):
        _runtime_with_v2(tmp_path, factories)

    assert [factory.opens for factory in factories] == [0, 0]
    assert [factory.key_reads for factory in factories] == [1, 1]
    assert [factory.identity_reads for factory in factories] == [1, 1]
    assert not (tmp_path / "atelier.sqlite").exists()
    assert not (tmp_path / "effects.sqlite").exists()


def test_v2_registry_without_an_unattended_capability_is_refused_before_open(
    tmp_path: Path,
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude/v1",
        "interactive-only",
        b"",
        capability_set=frozenset({AgentExecutionCapability.INTERACTIVE}),
    )

    with pytest.raises(ValueError, match="unattended capability"):
        _runtime_with_v2(tmp_path, (factory,))

    assert factory.opens == 0
    assert not (tmp_path / "atelier.sqlite").exists()
    assert not (tmp_path / "effects.sqlite").exists()


@pytest.mark.parametrize(
    "capability",
    (AgentExecutionCapability.HEADLESS, AgentExecutionCapability.HEADLESS_WITH_TOOLS),
)
def test_either_unattended_capability_alone_composes_a_v2_registry(
    tmp_path: Path, capability: AgentExecutionCapability
) -> None:
    """A tool-bearing executor may decline plain headless and still be composed.

    The runtime drives every attempt itself, so what it requires of an executor
    is that some unattended capability can reach it -- not that the tool-free
    one always can.
    """

    factory = RecordingAgentExecutorFactoryV2(
        "anthropic",
        "claude/v1",
        "one-unattended-operation",
        b"",
        capability_set=frozenset({capability}),
    )

    runtime = _runtime_with_v2(tmp_path, (factory,))
    try:
        assert runtime.agent_executor_registry.declared_capabilities(
            factory.key
        ) == frozenset({capability})
    finally:
        runtime.close()


def test_v2_factory_identity_is_captured_once_before_open(tmp_path: Path) -> None:
    factory = unstable_key_factory()
    runtime = _runtime_with_v2(tmp_path, (factory,))
    try:
        assert factory.key_reads == 1
        assert factory.identity_reads == 1
        assert factory.opens == 1
    finally:
        runtime.close()


def test_factory_open_never_enters_agent_invocation(tmp_path: Path) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude/v1", "operation", b"unused"
    )
    runtime = _runtime_with_v2(tmp_path, (factory,))
    try:
        assert factory.opens == 1
        assert factory.opened is not None
        assert factory.opened.requests == []
    finally:
        runtime.close()


def test_same_v2_factory_object_is_refused_without_durable_mutation(
    tmp_path: Path,
) -> None:
    factory = unstable_key_factory()
    runtime: DbosRuntime | None = None
    try:
        with pytest.raises(ValueError, match="factory objects must be unique"):
            runtime = _runtime_with_v2(tmp_path, (factory, factory))
    finally:
        if runtime is not None:
            runtime.close()

    assert factory.key_reads == 0
    assert factory.identity_reads == 0
    assert factory.opens == 0
    assert not (tmp_path / "atelier.sqlite").exists()
    assert not (tmp_path / "effects.sqlite").exists()


def test_partial_v2_open_failure_closes_prior_executor_and_releases_owner(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    opened = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude/v1", "claude-operation", b"", lifecycle
    )

    failing = failing_agent_executor_factory(
        "openai", lifecycle, open_failure=RuntimeError("injected V2 open failure")
    )

    with pytest.raises(RuntimeError, match="injected V2 open failure"):
        _runtime_with_v2(tmp_path, (failing, opened))

    assert lifecycle == ["open:anthropic", "open:openai", "close:anthropic"]
    assert opened.opened is not None
    assert opened.opened.closes == 1
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_v2_base_exception_open_closes_prior_executor_and_releases_owner(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    opened = RecordingAgentExecutorFactoryV2(
        "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
    )
    failure = KeyboardInterrupt("open:beta failed")

    with pytest.raises(KeyboardInterrupt) as captured:
        _runtime_with_v2(
            tmp_path,
            (
                opened,
                failing_agent_executor_factory("beta", lifecycle, open_failure=failure),
            ),
        )

    assert captured.value is failure
    assert lifecycle == ["open:alpha", "open:beta", "close:alpha"]
    assert opened.opened is not None
    assert opened.opened.closes == 1
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_v2_registration_failure_closes_all_opened_executors_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    factories = (
        RecordingAgentExecutorFactoryV2(
            "openai", "codex/v1", "codex-operation", b"", lifecycle
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude/v1", "claude-operation", b"", lifecycle
        ),
    )

    def fail_registration(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected registration failure")

    with monkeypatch.context() as context:
        context.setattr(
            "atelier2.adapters.dbos.runtime.register_durable_run_workflow",
            fail_registration,
        )
        with pytest.raises(RuntimeError, match="injected registration failure"):
            _runtime_with_v2(tmp_path, factories)

    assert lifecycle == [
        "open:anthropic",
        "open:openai",
        "close:openai",
        "close:anthropic",
    ]
    assert all(
        factory.opened is not None and factory.opened.closes == 1
        for factory in factories
    )
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_v2_open_and_cleanup_failures_preserve_original_then_cleanup_order(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []

    with pytest.raises(ExceptionGroup) as captured:
        _runtime_with_v2(
            tmp_path,
            (
                failing_agent_executor_factory(
                    "openai", lifecycle, open_failure=RuntimeError("open:openai failed")
                ),
                failing_agent_executor_factory(
                    "anthropic",
                    lifecycle,
                    close_failure=RuntimeError("cleanup:anthropic"),
                ),
            ),
        )

    assert lifecycle == ["open:anthropic", "open:openai", "close:anthropic"]
    assert [str(error) for error in captured.value.exceptions] == [
        "open:openai failed",
        "cleanup:anthropic",
    ]
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_base_exception_open_and_multiple_cleanup_failures_preserve_order(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    alpha_cleanup = KeyboardInterrupt("cleanup:alpha")
    beta_cleanup = SystemExit("cleanup:beta")
    original = KeyboardInterrupt("open:gamma")

    with pytest.raises(BaseExceptionGroup) as captured:
        _runtime_with_v2(
            tmp_path,
            (
                failing_agent_executor_factory(
                    "alpha", lifecycle, close_failure=alpha_cleanup
                ),
                failing_agent_executor_factory(
                    "beta", lifecycle, close_failure=beta_cleanup
                ),
                failing_agent_executor_factory(
                    "gamma", lifecycle, open_failure=original
                ),
            ),
        )

    assert lifecycle == [
        "open:alpha",
        "open:beta",
        "open:gamma",
        "close:beta",
        "close:alpha",
    ]
    assert captured.value.exceptions == (original, beta_cleanup, alpha_cleanup)
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_registration_base_exception_runs_all_cleanup_and_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    original = SystemExit("registration")
    factory = RecordingAgentExecutorFactoryV2(
        "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
    )

    def fail_registration(*_arguments: object, **_keywords: object) -> Never:
        lifecycle.append("register")
        raise original

    with monkeypatch.context() as context:
        context.setattr(
            "atelier2.adapters.dbos.runtime.register_durable_run_workflow",
            fail_registration,
        )
        with pytest.raises(SystemExit) as captured:
            _runtime_with_v2(tmp_path, (factory,))

    assert captured.value is original
    assert lifecycle == ["open:alpha", "register", "close:alpha"]
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_effect_open_base_exception_closes_provider_and_releases_owner(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    original = KeyboardInterrupt("effect open")
    factory = RecordingAgentExecutorFactoryV2(
        "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
    )
    effect_factory = BaseExceptionEffectFactory(
        EffectAdapterBinding(
            AdapterRevision("failing/v1"),
            EffectDestination("failing"),
            AdapterOperationalIdentity(str((tmp_path / "effects.sqlite").resolve())),
        ),
        original,
        lifecycle,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        DbosRuntime(
            DbosRuntimeSettings(
                tmp_path / "atelier.sqlite",
                "effect-open",
                agent_scratch_root=agent_scratch_root(tmp_path),
            ),
            effect_factory,
            (factory,),
        )

    assert captured.value is original
    assert lifecycle == ["open:alpha", "open:effect", "close:alpha"]
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_last_close_runs_every_v2_cleanup_and_releases_owner_despite_failures(
    tmp_path: Path,
) -> None:
    lifecycle: list[str] = []
    runtime = _runtime_with_v2(
        tmp_path,
        (
            failing_agent_executor_factory(
                "openai", lifecycle, close_failure=RuntimeError("cleanup:openai")
            ),
            failing_agent_executor_factory(
                "anthropic", lifecycle, close_failure=RuntimeError("cleanup:anthropic")
            ),
        ),
    )

    with pytest.raises(ExceptionGroup) as captured:
        runtime.close()

    assert lifecycle == [
        "open:anthropic",
        "open:openai",
        "close:openai",
        "close:anthropic",
    ]
    assert [str(error) for error in captured.value.exceptions] == [
        "cleanup:openai",
        "cleanup:anthropic",
    ]
    runtime.close()
    recovered = _runtime_with_v2(tmp_path, ())
    recovered.close()


def test_last_close_continues_after_base_exception_and_resets_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    factories = (
        RecordingAgentExecutorFactoryV2(
            "beta", "beta/v1", "beta-operation", b"", lifecycle
        ),
        RecordingAgentExecutorFactoryV2(
            "alpha", "alpha/v1", "alpha-operation", b"", lifecycle
        ),
    )
    runtime = _runtime_with_v2(tmp_path, factories)
    failure = SystemExit("effect close failed")
    disposed: list[str] = []
    original_dispose = runtime.engine.dispose

    def fail_effect_close() -> Never:
        lifecycle.append("close:effect")
        raise failure

    def dispose() -> None:
        disposed.append("engine")
        original_dispose()

    with monkeypatch.context() as context:
        context.setattr(runtime.effect_adapter, "close", fail_effect_close)
        context.setattr(runtime.engine, "dispose", dispose)
        with pytest.raises(SystemExit) as captured:
            runtime.close()

    assert captured.value is failure
    assert lifecycle == [
        "open:alpha",
        "open:beta",
        "close:effect",
        "close:beta",
        "close:alpha",
    ]
    assert disposed == ["engine"]
    assert all(
        factory.opened is not None and factory.opened.closes == 1
        for factory in factories
    )
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()


def test_last_close_aggregates_destroy_close_and_dispose_base_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    alpha_cleanup = KeyboardInterrupt("cleanup:alpha")
    beta_cleanup = SystemExit("cleanup:beta")
    runtime = _runtime_with_v2(
        tmp_path,
        (
            failing_agent_executor_factory(
                "alpha", lifecycle, close_failure=alpha_cleanup
            ),
            failing_agent_executor_factory(
                "beta", lifecycle, close_failure=beta_cleanup
            ),
        ),
    )
    destroy_failure = KeyboardInterrupt("destroy")
    effect_failure = SystemExit("cleanup:effect")
    dispose_failure = KeyboardInterrupt("dispose")

    def destroy(**_arguments: object) -> Never:
        lifecycle.append("destroy")
        raise destroy_failure

    def close_effect() -> Never:
        lifecycle.append("close:effect")
        raise effect_failure

    def dispose() -> Never:
        lifecycle.append("dispose")
        raise dispose_failure

    with monkeypatch.context() as context:
        context.setattr("atelier2.adapters.dbos.runtime.DBOS.destroy", destroy)
        context.setattr(runtime.effect_adapter, "close", close_effect)
        context.setattr(runtime.engine, "dispose", dispose)
        with pytest.raises(BaseExceptionGroup) as captured:
            runtime.close()

    assert lifecycle == [
        "open:alpha",
        "open:beta",
        "destroy",
        "close:effect",
        "close:beta",
        "close:alpha",
        "dispose",
    ]
    assert captured.value.exceptions == (
        destroy_failure,
        effect_failure,
        beta_cleanup,
        alpha_cleanup,
        dispose_failure,
    )
    recovered = _runtime_with_v2(tmp_path, (), application_version="recovered")
    recovered.close()
