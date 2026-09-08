"""Which effect intent no durable DBOS workflow is going to move again.

`effect_store.py` owns the write that converges each answer this module gives
-- WAITING_RECONCILIATION, an abandonment, or a reopened reconciliation door
-- but the question itself, "is anything still driving this intent", is read
-- only: DBOS's own `workflow_status` table, the node and run it was prepared
on, and nothing else. Splitting it out keeps that question answerable without
pulling in the writes its answer licenses.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum, auto
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.schema import effect_intents, runs
from atelier2.adapters.dbos.uncontinuable_runs import (
    LIVE_DRIVER_WORKFLOW_STATUSES,
    live_driver_workflow_ids,
)
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.contracts.effects import (
    EffectIntentState,
    LogicalEffectKey,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    logical_effect_key_for_node,
    logical_effect_key_for_work_item_claim,
)
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)

# DBOS owns this table and these tokens; the readers below only read them, and
# only to answer whether the workflow owing an intent its resolution raised.
# `uncontinuable_runs.py` owns whether one is a live driver under the running
# application version; this table exists only for that plain status read.
_dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("workflow_uuid"),
    sa.column("status"),
)
_TERMINAL_NON_SUCCESS_WORKFLOW_STATUSES = (
    "ERROR",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    "CANCELLED",
)
"""The DBOS statuses under which a workflow ended without committing its
resolution and will never run again: recovery replays only pending work, never
a raised ending. An absent row is not one of them -- absence says the workflow
was never written, and who owes it decides what that means."""

_TERMINAL_NODE_WORKFLOW_STATUSES = (*_TERMINAL_NON_SUCCESS_WORKFLOW_STATUSES, "SUCCESS")
"""The wider terminal set a node workflow answers to: unlike the effect or
reconcile workflow `_workflow_is_dead` otherwise checks, a node workflow's own
resolution is what commits its intents and enqueues its effect in the same
step, so SUCCESS discharges that duty just as finally as an error does."""

_DRIVEN_INTENT_STATES = (
    EffectIntentState.PREPARED.value,
    EffectIntentState.RECONCILING.value,
)
"""The intent states a durable workflow is currently responsible for moving."""


class DriverlessConvergence(Enum):
    """The ending owed to one intent no durable workflow will move again."""

    RECONCILIATION_DOOR = auto()
    """A prepared intent on a live run: lift that run to the operator's door."""

    ABANDONMENT = auto()
    """A prepared intent on a run that ended without it: write that ending."""

    REOPENED_DOOR = auto()
    """A reconciling intent whose command died: put it back behind the door."""


def driverless_effect_intents(
    engine: Engine, application_version: str
) -> tuple[LogicalEffectKey, ...]:
    """List candidates only; `_converge_intent` decides in its own write
    transaction, so a workflow that resolved between the two reads costs one
    extra read, never a wrong ending."""

    with engine.connect() as connection:
        candidates = (
            connection.execute(
                sa.select(effect_intents)
                .where(effect_intents.c.state.in_(_DRIVEN_INTENT_STATES))
                .order_by(effect_intents.c.logical_key)
            )
            .mappings()
            .all()
        )
        return tuple(
            LogicalEffectKey(str(record["logical_key"]))
            for record in candidates
            if driverless_convergence(connection, record, application_version)
            is not None
        )


def driverless_convergence(
    connection: Connection, record: Mapping[Any, Any], application_version: str
) -> DriverlessConvergence | None:
    """What this intent is owed, or nothing while a workflow still owes it.

    Only a PREPARED or a RECONCILING intent can be owed anything, because those
    are the two states a workflow is responsible for: the door already holds a
    WAITING one, a CONFIRMED one has its word, and an ABANDONED one has its
    run's.
    """

    state = EffectIntentState(str(record["state"]))
    if state is EffectIntentState.PREPARED:
        return _prepared_intent_convergence(connection, record, application_version)
    if state is EffectIntentState.RECONCILING and _workflow_is_dead(
        connection,
        reconcile_workflow_id_for(
            ReconcileCommandId(str(record["reconciliation_owner_command_id"]))
        ),
        application_version,
    ):
        return DriverlessConvergence.REOPENED_DOOR
    return None


def _prepared_intent_convergence(
    connection: Connection, record: Mapping[Any, Any], application_version: str
) -> DriverlessConvergence | None:
    """A prepared intent has two possible drivers, one of them not written yet.

    `durable_effect` resolves it, so its raised or version-stranded ending is
    the usual answer. Before that workflow exists there is still a driver:
    the Action node workflow commits the intent and enqueues the effect in
    the same step, so an absent effect row normally means recovery is about
    to write it. Only when that node workflow itself is dead does nobody owe
    this intent anything -- the window where it would stand PREPARED forever
    under no row the effect-workflow read could even name.

    What is owed then depends on the run: a live one is lifted to the operator
    door, and one that has already ended is written onto the intent instead.
    """

    logical_key = LogicalEffectKey(str(record["logical_key"]))
    effect_workflow_id = effect_workflow_id_for(logical_key)
    driverless = (
        _workflow_is_dead(connection, effect_workflow_id, application_version)
        if _workflow_status(connection, effect_workflow_id) is not None
        else _enqueueing_node_workflow_is_dead(
            connection, record, logical_key, application_version
        )
    )
    if not driverless:
        return None
    return _convergence_the_run_admits(connection, RunId(str(record["run_id"])))


def _convergence_the_run_admits(
    connection: Connection, run_id: RunId
) -> DriverlessConvergence | None:
    """Which ending this intent's own run still leaves open.

    A STARTED run is what the in-band UNKNOWN transition lifts, so it takes the
    door. A run that has ended cannot be lifted at all -- its terminal word and
    hash are written -- and before ABANDONED existed that left the intent
    standing PREPARED forever behind a door refusing it. Any other state
    is one a run holding a prepared effect cannot honestly stand in, because
    the run rests on its Action node until that effect confirms; nothing is
    converged for it rather than a word being guessed.
    """

    state = RunState(
        str(
            connection.execute(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            )
            .scalars()
            .one()
        )
    )
    if state is RunState.STARTED:
        return DriverlessConvergence.RECONCILIATION_DOOR
    if state in TERMINAL_RUN_STATES:
        return DriverlessConvergence.ABANDONMENT
    return None


def _enqueueing_node_workflow_is_dead(
    connection: Connection,
    record: Mapping[Any, Any],
    logical_key: LogicalEffectKey,
    application_version: str,
) -> bool:
    """Whether the Action node workflow that owes this intent its enqueue is gone.

    An intent does not name its node workflow and does not have to: it is
    prepared on the node its run is standing on, and the run stands there until
    the effect confirms -- an ending lifts the run where it stands and moves it
    no further, so a run that ended still names the node this intent belongs
    to. That node owes two possible intents in the same step -- its own effect,
    keyed by `logical_effect_key_for_node`, and the work-item claim it takes
    before it runs, keyed by `logical_effect_key_for_work_item_claim` (the same
    split `_agent_redeemed_owning_workflow_ids` reads for the restart sweep).
    Requiring this intent's key to be exactly one of the two is what makes the
    derived workflow id this intent's driver rather than a neighbour's; when it
    is neither, nothing here can honestly name a driver, so the intent is left
    alone.
    """

    run = (
        connection.execute(
            sa.select(runs.c.current_node_id, runs.c.current_round_ordinal).where(
                runs.c.run_id == str(record["run_id"])
            )
        )
        .mappings()
        .one()
    )
    run_id = RunId(str(record["run_id"]))
    revision_hash = WorkflowRevisionHash(str(record["workflow_revision_hash"]))
    node_id = str(run["current_node_id"])
    round_ordinal = int(run["current_round_ordinal"])
    if logical_key not in (
        logical_effect_key_for_node(run_id, revision_hash, node_id, round_ordinal),
        logical_effect_key_for_work_item_claim(
            run_id, revision_hash, node_id, round_ordinal
        ),
    ):
        return False
    execution_id = NodeExecutionId.for_node(
        run_id, revision_hash, node_id, round_ordinal
    )
    return _workflow_is_dead(
        connection,
        node_workflow_id_for(execution_id),
        application_version,
        terminal_statuses=_TERMINAL_NODE_WORKFLOW_STATUSES,
    )


def _workflow_is_dead(
    connection: Connection,
    workflow_id: str,
    application_version: str,
    *,
    terminal_statuses: tuple[str, ...] = _TERMINAL_NON_SUCCESS_WORKFLOW_STATUSES,
) -> bool:
    """Whether DBOS itself will never take this workflow another step.

    A terminal error status is the ordinary answer; `terminal_statuses` lets
    a node-workflow caller widen that set to SUCCESS, since a node's own
    successful resolution discharges it just as finally. A workflow still
    PENDING, ENQUEUED, or DELAYED under a retired `application_version` is
    just as dead: DBOS scopes recovery to the version that enqueued it, so a
    deploy that retires that version strands the workflow exactly as if it
    had raised. An absent row is neither -- it was never written, and who
    owes it decides what that means, not this predicate.
    """

    status = _workflow_status(connection, workflow_id)
    if status in terminal_statuses:
        return True
    if status not in LIVE_DRIVER_WORKFLOW_STATUSES:
        return False
    return workflow_id not in live_driver_workflow_ids(
        connection, (workflow_id,), application_version
    )


def _workflow_status(connection: Connection, workflow_id: str) -> str | None:
    status = connection.scalar(
        sa.select(_dbos_workflow_status.c.status).where(
            _dbos_workflow_status.c.workflow_uuid == workflow_id
        )
    )
    return None if status is None else str(status)
