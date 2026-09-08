"""Serve-start convergence for effect intents no durable workflow will move.

A raised adapter exception -- a GitHub 500, a timeout -- ends `durable_effect`
or `durable_reconciliation` in a terminal DBOS error status, and recovery
replays only pending work, never a raised ending. Before this sweep the intent
stood PREPARED or RECONCILING forever, the operator door refused it, and the
run said STARTED until the store died (#628). An Action node workflow that ends
terminally between committing its intent and enqueuing the effect leaves the
same frozen intent under no effect-workflow row at all (#646). A workflow left
PENDING under a retired `application_version` is frozen the same way -- DBOS
will never resume it, so reading it as a live driver anyway leaves the run
frozen forever (#707). This file prepares those exact rows, models each ending
as the `workflow_status` row DBOS leaves behind, and asks the sweep to route
only what no live workflow owes: to WAITING_RECONCILIATION, onto the attention
feed, and through the operator door -- never to an invented absence (ADR 0010).

Routing to that door lifts a STARTED run, so a run that had already ended took
none of it: the intent stayed PREPARED forever behind a door refusing it, and
the sweep's own transition raised on the terminal run rather than converging
(#705). ABANDONED is what such an intent gets instead, and the tests below ask
for exactly what that word may cost: the run keeps its ending and gains no
event, the operator door answers stale rather than opening, a second sweep is a
no-op, and a driver that comes back writes no receipt over the ending.

The key the sweep matches against is round-bound (ADR 0014): `graph_action_
intent` used to mint it from round one's execution id unconditionally, so a
run a declared loop had carried past round one would compare a round-aware
recomputation against a key that still named round one and leave the intent
alone rather than route it (#706). `logical_effect_key_for_node` is now the
one owner both the preparer and this sweep call, so the two can never drift
apart again. No published document can place an Action inside a declared
loop's body -- `_unrepeatable_loop_forms` refuses that by name at every load
of the document -- so no fixture here drives a genuinely round-two Action end
to end; a run standing on a round-two Action is not a state this runtime can
honestly reach at all. The tests below pin what is provable instead: round
one stays the exact bytes every stored intent already carries, a later round
mints its own key from the same node, and -- read as a conservative-sweep
safety property against a malformed row rather than round-two Action evidence
-- a key that disagrees with the round its run's own head records is never
mistaken for the one it stands in now.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.advancer import prepared_effect_intent
from atelier2.adapters.dbos.effect_store import (
    DurableEffectConflict,
    commit_resolution,
    converge_driverless_effect_intents,
    encode_found,
)
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_transitions import lift_started_run
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    run_events,
    runs,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.uncontinuable_runs import LIVE_DRIVER_WORKFLOW_STATUSES
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationExistingRejected,
    ReconciliationStale,
    reconcile_effect_result,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effects import (
    EFFECT_INTENT_VERSION_ABANDONED,
    ConfirmationSource,
    EffectBinding,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    LogicalEffectKey,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for,
    logical_effect_key_for_node,
    logical_effect_key_for_work_item_claim,
)
from atelier2.contracts.run_projections import NodeState, RunPage
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.run_events import AttentionEvent, AttentionEventPage
from atelier2.ports.run_queries import NodeDetailFound, RunFound
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.api import durable_queries, healthy_runs
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
)
from tests.scenarios.runtime import recording_exact_runtime
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    V3_EFFECT_LINE_ACTION_NODE_ID,
    V3_EFFECT_LINE_AGENT_JOB,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
)

WORKFLOW_DOCUMENT = V3_EFFECT_LINE_DOCUMENT
PROVIDER_OUTPUT = b'"request"'
ACTION_NODE_ID = V3_EFFECT_LINE_ACTION_NODE_ID
"""The node of WORKFLOW_DOCUMENT the prepared intent belongs to."""


@pytest.fixture
def prepared(tmp_path: Path) -> Iterator[tuple[DbosRuntime, EffectIntent]]:
    runtime = recording_exact_runtime(
        canonical_runtime_settings(
            tmp_path, "executor-A", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        PROVIDER_OUTPUT,
    )
    runtime.initialize_storage()
    revision = WorkflowRevision(WORKFLOW_DOCUMENT)
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        revision,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        RunId("run-1"),
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        PROVIDER_OUTPUT,
    )
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        revision.revision_hash,
        runtime.effect_adapter_binding,
    )
    try:
        yield runtime, intent
    finally:
        runtime.close()


def command(intent: EffectIntent, command_id: str = "command-1") -> ReconcileCommand:
    return ReconcileCommand(
        ReconcileCommandId(command_id),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected the exact destination and request",
        OperatorFoundEffect(EffectId("external-1"), EffectResult(b"result")),
    )


def end_workflow(engine: Engine, workflow_id: str, status: str = "ERROR") -> None:
    """Leave the row DBOS leaves when a workflow raises instead of crashing."""

    with engine.begin() as connection:
        ended = connection.execute(
            sa.text(
                "UPDATE workflow_status SET status=:status "
                "WHERE workflow_uuid=:workflow_id"
            ),
            {"status": status, "workflow_id": workflow_id},
        )
        assert ended.rowcount == 1


def strand_under_a_retired_application_version(
    engine: Engine, workflow_id: str, status: str
) -> None:
    """Leave the row DBOS leaves once a deploy retires the version driving it.

    `end_workflow` models a workflow that raised; this models the #707
    ending: still PENDING, ENQUEUED, or DELAYED -- DBOS would resume it -- but
    under an `application_version` this instance no longer runs, so DBOS
    itself never will.
    """

    with engine.begin() as connection:
        updated = connection.execute(
            sa.text(
                "UPDATE workflow_status SET status=:status, "
                "application_version=:application_version "
                "WHERE workflow_uuid=:workflow_id"
            ),
            {
                "status": status,
                "application_version": "dead-application-version",
                "workflow_id": workflow_id,
            },
        )
        assert updated.rowcount == 1


def leave_workflow(
    engine: Engine, workflow_id: str, status: str, application_version: str
) -> None:
    """Leave the row DBOS leaves for a workflow standing in that status.

    The Action node workflow the fixture's intent was prepared by never ran
    here -- the fixture calls that preparation step directly -- so its row is
    written rather than updated. A caller names `application_version`
    explicitly rather than relying on a default so a test that plants a
    workflow under a retired deploy (#707) reads as deliberate, not assumed.
    """

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workflow_status "
                "(workflow_uuid,status,created_at,updated_at,priority,"
                "application_version) "
                "VALUES (:workflow_id,:status,0,0,0,:application_version)"
            ),
            {
                "workflow_id": workflow_id,
                "status": status,
                "application_version": application_version,
            },
        )


def action_node_workflow_id(intent: EffectIntent) -> str:
    return node_workflow_id_for(
        NodeExecutionId.for_node(
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            ACTION_NODE_ID,
        )
    )


def prepare_claim_intent(runtime: DbosRuntime, intent: EffectIntent) -> EffectIntent:
    """Prepare ACTION_NODE_ID's own work-item claim beside its effect intent.

    Recorded through `prepared_effect_intent`, the same production entry point
    `prepare_work_item_claim` calls, from the exact binding fields `prepared`
    already recorded for the node's own effect intent -- only the logical key
    (`logical_effect_key_for_work_item_claim` rather than `logical_effect_key_
    for_node`) and the operation name differ. That is exactly the one
    difference `_enqueueing_node_workflow_is_dead` must tell apart.
    """

    claim_binding = EffectBinding(
        logical_effect_key_for_work_item_claim(
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            ACTION_NODE_ID,
        ),
        intent.binding.run_id,
        intent.binding.workflow_revision_hash,
        intent.binding.adapter_revision,
        intent.binding.destination,
        intent.binding.adapter_operational_identity,
        AdapterOperationName.CLAIM_WORK_ITEM,
    )
    claim_intent = EffectIntent(claim_binding, intent.request)
    with canonical_write_transaction(runtime.engine) as connection:
        prepared_effect_intent(connection, claim_intent)
    return claim_intent


def converge(engine: Engine, application_version: str) -> tuple[LogicalEffectKey, ...]:
    return converge_driverless_effect_intents(engine, application_version)


def intent_row(
    engine: Engine, logical_key: LogicalEffectKey | None = None
) -> tuple[object, ...]:
    """The fixture's one intent row, or a named one once more than one exists."""

    query = sa.select(
        effect_intents.c.state,
        effect_intents.c.state_version,
        effect_intents.c.reconciliation_owner_command_id,
    )
    if logical_key is not None:
        query = query.where(effect_intents.c.logical_key == logical_key.value)
    with engine.connect() as connection:
        return tuple(connection.execute(query).one())


def run_event_kinds(engine: Engine) -> tuple[str, ...]:
    with engine.connect() as connection:
        return tuple(
            str(kind)
            for kind in connection.execute(
                sa.select(run_events.c.event_kind).order_by(run_events.c.event_sequence)
            ).scalars()
        )


ABANDONED_ROW = (
    EffectIntentState.ABANDONED.value,
    EFFECT_INTENT_VERSION_ABANDONED.value,
    None,
)
"""The exact intent row an abandonment leaves: the word, its one advance,
and no owning command -- there is no reconciliation to own it."""


def route_to_waiting(runtime: DbosRuntime, intent: EffectIntent) -> None:
    end_workflow(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))
    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )


def effect_workflow_ended(status: str) -> Callable[[DbosRuntime, EffectIntent], None]:
    def leave(runtime: DbosRuntime, intent: EffectIntent) -> None:
        end_workflow(
            runtime.engine, effect_workflow_id_for(intent.binding.logical_key), status
        )

    return leave


def node_workflow_ended_before_the_enqueue(
    runtime: DbosRuntime, intent: EffectIntent
) -> None:
    """The #646 window: the intent is committed, its enqueue never happened."""

    _drop_workflow(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))
    leave_workflow(
        runtime.engine,
        action_node_workflow_id(intent),
        "ERROR",
        runtime.settings.application_version,
    )


def effect_workflow_pending_under_a_retired_application_version(
    status: str,
) -> Callable[[DbosRuntime, EffectIntent], None]:
    """#707, the direct driver: PREPARED reads `_workflow_is_dead` straight off
    the effect workflow (`effect_store.py`) whenever that row already exists."""

    def leave(runtime: DbosRuntime, intent: EffectIntent) -> None:
        strand_under_a_retired_application_version(
            runtime.engine, effect_workflow_id_for(intent.binding.logical_key), status
        )

    return leave


def node_workflow_pending_under_a_retired_application_version(
    status: str,
) -> Callable[[DbosRuntime, EffectIntent], None]:
    """#707, the #646 window's driver: no effect workflow was ever enqueued,
    so `_enqueueing_node_workflow_is_dead` reads the Action node workflow
    instead. Mirrors `test_converge_ends_a_silent_successor_whose_durable_
    workflow_is_the_dead_version` (#645, `test_interrupted_uncontinuable_
    inventory.py`): dropping the `application_version` match would read
    either workflow as a live driver and leave the run frozen forever.
    """

    def leave(runtime: DbosRuntime, intent: EffectIntent) -> None:
        _drop_workflow(
            runtime.engine, effect_workflow_id_for(intent.binding.logical_key)
        )
        leave_workflow(
            runtime.engine,
            action_node_workflow_id(intent),
            status,
            "dead-application-version",
        )

    return leave


@pytest.mark.parametrize(
    "leave_dead_driver",
    [
        pytest.param(effect_workflow_ended("ERROR"), id="effect-workflow-raised"),
        pytest.param(
            effect_workflow_ended("MAX_RECOVERY_ATTEMPTS_EXCEEDED"),
            id="effect-workflow-exhausted-recovery",
        ),
        pytest.param(
            effect_workflow_ended("CANCELLED"), id="effect-workflow-cancelled"
        ),
        pytest.param(
            node_workflow_ended_before_the_enqueue,
            id="node-workflow-ended-before-the-enqueue",
        ),
        *[
            pytest.param(
                effect_workflow_pending_under_a_retired_application_version(status),
                id=f"effect-workflow-{status.lower()}-under-a-retired-application-version",
            )
            for status in LIVE_DRIVER_WORKFLOW_STATUSES
        ],
        *[
            pytest.param(
                node_workflow_pending_under_a_retired_application_version(status),
                id=f"node-workflow-{status.lower()}-under-a-retired-application-version",
            )
            for status in LIVE_DRIVER_WORKFLOW_STATUSES
        ],
    ],
)
def test_prepared_intent_no_workflow_will_move_reaches_the_operator_door(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_dead_driver: Callable[[DbosRuntime, EffectIntent], None],
) -> None:
    runtime, intent = prepared
    leave_dead_driver(runtime, intent)

    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )

    assert intent_row(runtime.engine) == (
        EffectIntentState.WAITING_RECONCILIATION.value,
        1,
        None,
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(runs.c.state))
            == RunState.WAITING_RECONCILIATION.value
        )
        assert connection.execute(
            sa.select(run_events.c.node_id, run_events.c.payload).where(
                run_events.c.event_kind
                == RunEventKind.ACTION_RECONCILIATION_REQUIRED.value
            )
        ).one() == (ACTION_NODE_ID, intent.request.payload)

    page = durable_queries(runtime.engine).read_attention_event_page(None, None, 10, ())
    assert isinstance(page, AttentionEventPage)
    assert len(page.events) == 1
    item = page.events[0]
    assert isinstance(item, AttentionEvent)
    assert (item.event.event.run_id, item.event.event.event_kind) == (
        RunId("run-1"),
        RunEventKind.ACTION_RECONCILIATION_REQUIRED,
    )

    accepted = reconcile_effect_result(
        command(intent), DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    )
    assert isinstance(accepted, ReconciliationAcceptedPending)


def drop_effect_workflow(runtime: DbosRuntime, intent: EffectIntent) -> None:
    _drop_workflow(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))


def node_workflow_still_owes_the_enqueue(
    runtime: DbosRuntime, intent: EffectIntent
) -> None:
    drop_effect_workflow(runtime, intent)
    leave_workflow(
        runtime.engine,
        action_node_workflow_id(intent),
        "PENDING",
        runtime.settings.application_version,
    )


@pytest.mark.parametrize(
    "leave_live_driver",
    [
        pytest.param(lambda runtime, intent: None, id="effect-workflow-still-enqueued"),
        pytest.param(
            drop_effect_workflow, id="no-workflow-row-recovery-owes-the-enqueue"
        ),
        pytest.param(
            node_workflow_still_owes_the_enqueue,
            id="node-workflow-still-owes-the-enqueue",
        ),
    ],
)
def test_prepared_intent_still_owed_a_driver_is_left_alone(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_live_driver: Callable[[DbosRuntime, EffectIntent], None],
) -> None:
    runtime, intent = prepared
    leave_live_driver(runtime, intent)

    assert converge(runtime.engine, runtime.settings.application_version) == ()

    assert intent_row(runtime.engine) == (EffectIntentState.PREPARED.value, 0, None)
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(runs.c.state)) == RunState.STARTED.value


def test_a_still_running_nodes_claim_intent_is_left_alone(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    """A claim key must match its own live node, not just a dead one.

    `node_workflow_still_owes_the_enqueue` is the exact live-node shape
    `test_prepared_intent_still_owed_a_driver_is_left_alone` already proves
    for the node's own effect key; this asks the same question of the claim
    key sharing that node.
    """

    runtime, intent = prepared
    claim = prepare_claim_intent(runtime, intent)
    node_workflow_still_owes_the_enqueue(runtime, intent)

    assert converge(runtime.engine, runtime.settings.application_version) == ()

    assert intent_row(runtime.engine, claim.binding.logical_key) == (
        EffectIntentState.PREPARED.value,
        0,
        None,
    )


def end_the_run(runtime: DbosRuntime, intent: EffectIntent, ending: RunState) -> None:
    """Close the run where it stands, the way serve-start's inventory does.

    `lift_started_run` is the one owner of that ending -- the same call
    `uncontinuable_runs` makes -- so the run really carries a terminal word and
    the hash folded over its own log, rather than a state string written by
    hand into a row no transition ever produced.
    """

    with canonical_write_transaction(runtime.engine) as connection:
        run = (
            connection.execute(
                sa.select(runs.c.state_version, runs.c.last_event_sequence).where(
                    runs.c.run_id == intent.binding.run_id.value
                )
            )
            .mappings()
            .one()
        )
        assert lift_started_run(
            connection,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            int(run["state_version"]),
            int(run["last_event_sequence"]),
            ending,
        )


def abandon(runtime: DbosRuntime, intent: EffectIntent, ending: RunState) -> None:
    """The whole #705 shape: no driver left, the run ended, the sweep run."""

    node_workflow_ended_before_the_enqueue(runtime, intent)
    end_the_run(runtime, intent, ending)
    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )


@pytest.mark.parametrize(
    "ending", [RunState.FAILED, RunState.CANCELLED, RunState.COMPLETED]
)
@pytest.mark.parametrize(
    "leave_dead_driver",
    [
        pytest.param(effect_workflow_ended("ERROR"), id="effect-workflow-raised"),
        pytest.param(
            node_workflow_ended_before_the_enqueue,
            id="node-workflow-ended-before-the-enqueue",
        ),
    ],
)
def test_prepared_intent_on_a_run_that_already_ended_is_abandoned(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_dead_driver: Callable[[DbosRuntime, EffectIntent], None],
    ending: RunState,
) -> None:
    """The run's ending is written onto the intent, and nothing else moves.

    No door opens, because opening one lifts a live run and this one is over;
    no event is appended, because the run's terminal event is already the last
    word its hash folds over; the prepared request bytes stay exactly where an
    operator can still read what was about to be sent.
    """

    runtime, intent = prepared
    leave_dead_driver(runtime, intent)
    end_the_run(runtime, intent, ending)
    events_before = run_event_kinds(runtime.engine)

    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )

    assert intent_row(runtime.engine) == ABANDONED_ROW
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(runs.c.state)) == ending.value
        assert (
            connection.scalar(sa.select(effect_intents.c.canonical_request))
            == intent.request.payload
        )
    assert run_event_kinds(runtime.engine) == events_before


def test_a_dead_nodes_claim_intent_on_an_ended_run_is_abandoned(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    """The live store's own shape: a claim key on a node that ended SUCCESS,
    on a run that has already ended.

    `_enqueueing_node_workflow_is_dead` used to compare only against
    `logical_effect_key_for_node`, so a work-item claim intent never matched
    its own node and stood PREPARED forever -- even once the node ended
    SUCCESS and the run itself ended too. This proves both halves of the fix
    together: the claim key is recognised as the node's own, and a SUCCESS
    ending is read as dead exactly like a raised one.
    """

    runtime, intent = prepared
    claim = prepare_claim_intent(runtime, intent)
    _drop_workflow(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))
    leave_workflow(
        runtime.engine,
        action_node_workflow_id(intent),
        "SUCCESS",
        runtime.settings.application_version,
    )
    end_the_run(runtime, intent, RunState.FAILED)

    converged = converge(runtime.engine, runtime.settings.application_version)

    assert set(converged) == {intent.binding.logical_key, claim.binding.logical_key}
    assert intent_row(runtime.engine, intent.binding.logical_key) == ABANDONED_ROW
    assert intent_row(runtime.engine, claim.binding.logical_key) == ABANDONED_ROW


def test_an_abandoned_intent_is_not_swept_a_second_time(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    """Every restart runs this sweep, so its second run must cost nothing."""

    runtime, intent = prepared
    abandon(runtime, intent, RunState.FAILED)

    assert converge(runtime.engine, runtime.settings.application_version) == ()

    assert intent_row(runtime.engine) == ABANDONED_ROW


def test_the_operator_door_answers_an_abandoned_intent_stale(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    """There is nothing left to reconcile, and the door says so by name.

    A reconciliation resolves an intent *and* lifts its run back out of
    WAITING_RECONCILIATION. Neither is available here, so an operator command
    is recorded and refused rather than accepted into a run that has ended.
    """

    runtime, intent = prepared
    abandon(runtime, intent, RunState.FAILED)

    answered = reconcile_effect_result(
        command(intent), DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    )

    assert answered == ReconciliationStale()
    assert intent_row(runtime.engine) == ABANDONED_ROW


def test_a_returning_driver_writes_no_receipt_over_an_abandonment(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    """The CAS is what makes abandoning a live-looking intent safe.

    Nothing stops the workflow this sweep read as dead from committing one more
    time -- a recovery nobody predicted, a status read a moment too early. Its
    resolution must lose loudly and leave no trace, because a receipt written
    over an ending would say the destination answered a run that was over.
    """

    runtime, intent = prepared
    abandon(runtime, intent, RunState.FAILED)
    performed = PerformedEffect(EffectId("external-1"), EffectResult(b"result"))

    # The transaction is named second so it is the inner context and sees the
    # raise: that rollback is the "leave no trace" half of what is under test.
    with (
        pytest.raises(DurableEffectConflict),
        canonical_write_transaction(runtime.engine) as connection,
    ):
        commit_resolution(
            connection,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
            encode_found(performed, ConfirmationSource.ADAPTER_EXECUTION),
        )

    assert intent_row(runtime.engine) == ABANDONED_ROW
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_receipts))
            == 0
        )


@pytest.mark.parametrize("ending", [RunState.FAILED, RunState.CANCELLED])
def test_an_abandoned_intent_is_projected_on_the_ended_run(
    prepared: tuple[DbosRuntime, EffectIntent],
    ending: RunState,
) -> None:
    """The run's own read names the word the store wrote, including the request.

    Hop 38 made ABANDONED durable and left it off every public run shape. A
    reader asking for the run after the sweep must see the intent, or the
    prepared bytes stay readable only by opening the store.

    COMPLETED is omitted here: lifting a V1 run to COMPLETED while it still
    stands on the Action is a store-side inventory ending, and the read already
    refuses it as not the sink. FAILED and CANCELLED are the endings a reader
    of this document can honestly be told.
    """

    runtime, intent = prepared
    abandon(runtime, intent, ending)

    found = durable_queries(runtime.engine).get_run(intent.binding.run_id)

    assert isinstance(found, RunFound)
    reconciliation = found.projection.reconciliation
    assert reconciliation is not None
    assert reconciliation.intent.state is EffectIntentState.ABANDONED
    assert reconciliation.pending_command is None
    assert reconciliation.intent.intent.request.payload == intent.request.payload
    assert found.projection.run.state is ending

    detail = durable_queries(runtime.engine).get_node_detail(
        intent.binding.run_id, ACTION_NODE_ID
    )
    assert isinstance(detail, NodeDetailFound)
    assert detail.detail.refusal == EffectIntentState.ABANDONED.value
    assert detail.detail.state is (
        NodeState.FAILED if ending is RunState.FAILED else NodeState.CANCELLED
    )


@pytest.mark.parametrize("ending", [RunState.FAILED, RunState.CANCELLED])
def test_an_ended_action_run_does_not_acquire_abandoned_without_a_matching_intent(
    prepared: tuple[DbosRuntime, EffectIntent],
    ending: RunState,
) -> None:
    """ABANDONED is the stored word, not an inference from the run's ending.

    Before hop 38 a prepared intent on an ended Action stayed PREPARED. That
    leftover is still a legal row, and a reader of this run must keep the
    ending without inventing ABANDONED because the node is an Action.
    """

    runtime, intent = prepared
    node_workflow_ended_before_the_enqueue(runtime, intent)
    end_the_run(runtime, intent, ending)

    queries = durable_queries(runtime.engine)
    found = queries.get_run(intent.binding.run_id)
    listed = queries.list_runs(None, 10)

    assert intent_row(runtime.engine) == (EffectIntentState.PREPARED.value, 0, None)
    assert isinstance(found, RunFound)
    assert found.projection.reconciliation is None
    assert found.projection.run.state is ending
    assert isinstance(listed, RunPage)
    listed_run = healthy_runs(listed)[0]
    assert listed_run.reconciliation is None
    assert listed_run.run.state is ending

    detail = queries.get_node_detail(intent.binding.run_id, ACTION_NODE_ID)
    assert isinstance(detail, NodeDetailFound)
    assert detail.detail.refusal is None
    assert detail.detail.state is (
        NodeState.FAILED if ending is RunState.FAILED else NodeState.CANCELLED
    )


def _drop_workflow(engine: Engine, workflow_id: str) -> None:
    with engine.begin() as connection:
        dropped = connection.execute(
            sa.text("DELETE FROM workflow_status WHERE workflow_uuid=:workflow_id"),
            {"workflow_id": workflow_id},
        )
        assert dropped.rowcount == 1


def reconcile_workflow_ended(status: str) -> Callable[[Engine, str], None]:
    def leave(engine: Engine, workflow_id: str) -> None:
        end_workflow(engine, workflow_id, status)

    return leave


def reconcile_workflow_pending_under_a_retired_application_version(
    status: str,
) -> Callable[[Engine, str], None]:
    """#707, the RECONCILING driver: `_intent_is_driverless` reads
    `_workflow_is_dead` straight off the reconcile workflow."""

    def leave(engine: Engine, workflow_id: str) -> None:
        strand_under_a_retired_application_version(engine, workflow_id, status)

    return leave


@pytest.mark.parametrize(
    "leave_dead_reconcile_workflow",
    [
        pytest.param(reconcile_workflow_ended("ERROR"), id="reconcile-workflow-raised"),
        *[
            pytest.param(
                reconcile_workflow_pending_under_a_retired_application_version(status),
                id=f"reconcile-workflow-{status.lower()}-under-a-retired-application-version",
            )
            for status in LIVE_DRIVER_WORKFLOW_STATUSES
        ],
    ],
)
def test_reconciling_intent_whose_reconcile_workflow_is_dead_reopens_the_door(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_dead_reconcile_workflow: Callable[[Engine, str], None],
) -> None:
    runtime, intent = prepared
    route_to_waiting(runtime, intent)
    dead = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, dead)
    leave_dead_reconcile_workflow(
        runtime.engine, reconcile_workflow_id_for(dead.command_id)
    )

    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )

    assert intent_row(runtime.engine) == (
        EffectIntentState.WAITING_RECONCILIATION.value,
        1,
        None,
    )
    commander = DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    assert reconcile_effect_result(dead, commander) == ReconciliationExistingRejected(
        ReconcileCommandSnapshot(dead, ReconcileCommandState.REJECTED_CONFLICT)
    )
    fresh = reconcile_effect_result(command(intent, "command-2"), commander)
    assert isinstance(fresh, ReconciliationAcceptedPending)


def test_reconciling_intent_whose_reconcile_workflow_is_enqueued_is_left_alone(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    runtime, intent = prepared
    route_to_waiting(runtime, intent)
    pending = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, pending)

    assert converge(runtime.engine, runtime.settings.application_version) == ()

    assert intent_row(runtime.engine) == (
        EffectIntentState.RECONCILING.value,
        2,
        pending.command_id.value,
    )


def test_action_effect_key_is_round_one_by_the_same_bytes_the_store_already_holds() -> (
    None
):
    """#706, the no-migration half: round one moves nothing already stored.

    `logical_effect_key_for_node` is the one owner `graph_action_intent` and
    the #646 sweep both call now, but it must still answer round one exactly
    as the un-rounded call every durable intent already carries did -- or
    every Action effect this store has ever prepared would key differently
    the next time it is recomputed.
    """
    revision_hash = WorkflowRevision(WORKFLOW_DOCUMENT).revision_hash
    run_id = RunId("run-1")

    assert logical_effect_key_for_node(
        run_id, revision_hash, ACTION_NODE_ID
    ) == logical_effect_key_for(
        NodeExecutionId.for_node(run_id, revision_hash, ACTION_NODE_ID)
    )


def test_action_effect_key_mints_its_own_value_a_round_later() -> None:
    """#706, the round-aware half: a later round is never round one's key.

    `graph_action_intent` used to derive an Action's key from round one's
    execution id no matter which round the run actually stood in, so a run a
    declared loop had carried past round one minted the same key round one
    would have -- and the #646 sweep, recomputing from the run's own round,
    would never find a driverless intent honestly its own. This is the
    contract that closes that gap: every round mints a distinct key from the
    same node.
    """
    revision_hash = WorkflowRevision(WORKFLOW_DOCUMENT).revision_hash
    run_id = RunId("run-1")

    round_one = logical_effect_key_for_node(run_id, revision_hash, ACTION_NODE_ID)
    round_two = logical_effect_key_for_node(run_id, revision_hash, ACTION_NODE_ID, 2)

    assert round_two != round_one


def test_prepared_intent_whose_key_disagrees_with_a_malformed_run_round_is_left_alone(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    """Conservative-sweep safety, not round-two Action evidence.

    No document may declare an Action inside a loop's body
    (`_unrepeatable_loop_forms` refuses it at every load of the document), and
    `_require_a_round_the_graph_declares` refuses a stored run whose round
    disagrees with what its own document permits -- so a run genuinely
    standing on a round-two Action cannot exist here, or anywhere in this
    runtime, today. The raw `UPDATE` below deliberately produces exactly that
    malformed row, outside every domain constructor that would refuse it, to
    answer a narrower question: if a round ever disagreed with what a stored
    intent's key names -- through this defect, a future one, or plain
    corruption -- does the sweep stay conservative rather than paper over the
    disagreement? Mirrors `test_prepared_intent_no_workflow_will_move_
    reaches_the_operator_door`'s #646 window (a dead Action node workflow, no
    effect-workflow row at all); the one difference is the malformed round.
    The sweep must still leave the intent exactly where #646 already leaves
    one whose driver is still owed -- never a silent confirmation under a
    round the key does not name.
    """
    runtime, intent = prepared
    node_workflow_ended_before_the_enqueue(runtime, intent)
    with runtime.engine.begin() as connection:
        moved = connection.execute(
            sa.text("UPDATE runs SET current_round_ordinal=2 WHERE run_id=:run_id"),
            {"run_id": intent.binding.run_id.value},
        )
        assert moved.rowcount == 1

    assert converge(runtime.engine, runtime.settings.application_version) == ()

    assert intent_row(runtime.engine) == (EffectIntentState.PREPARED.value, 0, None)
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(runs.c.state)) == RunState.STARTED.value
