"""The durable run row, its graph binding, and the state transitions written on it.

One owner, below every store that has to move a run. The effect store resolves an
Action and must say the run is now reconciling, or reconciling no longer; the run
store, the attempt store, the advancer, the starter and the read queries all
decode the same row and write through the same event seam. While these lived
beside one of those stores, the modules underneath could reach them only from
inside a function body -- an import cycle hidden rather than removed.

What stays above this module is every writer that also writes a row of its own --
an agent receipt, a wait answer, an effect receipt. Those belong to the store that
owns that row. What belongs here is the run's own state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.agent_catalog import (
    agent_configuration_from_record,
    auth_profile_from_record,
)
from atelier2.adapters.dbos.instants import record_event_instant, record_run_ended
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    auth_profile_revisions,
    run_agent_bindings,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptReplacement,
)
from atelier2.contracts.agents import (
    AgentBindingSetHash,
    AgentReceiptHash,
    AgentRole,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    TransitionSnapshot,
    WaitAnswerActor,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import AnyRun, RunV2, RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    TERMINAL_RUN_STATES,
    RevisionHashCollision,
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocument,
    WaitNodeV3,
    WorkflowGraphV3,
    is_sink_node,
)


class RunTransitionConflict(RuntimeError):
    """A retry or transition contradicts the exact durable graph/run/event binding."""


def load_graph(
    session: Any, revision_hash: WorkflowRevisionHash
) -> AnyWorkflowDocument:
    document = session.scalar(
        sa.select(workflow_revisions.c.document).where(
            workflow_revisions.c.revision_hash == revision_hash.value
        )
    )
    if document is None:
        raise RunTransitionConflict("workflow revision is missing")
    return graph_from_document(revision_hash, bytes(document))


def graph_from_document(
    revision_hash: WorkflowRevisionHash, document: bytes
) -> AnyWorkflowDocument:
    revision = WorkflowRevision(bytes(document))
    if revision.revision_hash != revision_hash:
        raise RevisionHashCollision(
            "durable workflow revision bytes disagree with their hash"
        )
    return parse_executable_workflow_document(revision.document)


def run_from_record(record: Mapping[Any, Any]) -> Run:
    if (
        WorkflowFormatVersion(int(record["workflow_format_version"]))
        is not WorkflowFormatVersion.V1
        or record["agent_binding_set_hash"] is not None
    ):
        raise RunTransitionConflict("V1 run record carries a V2 binding")
    terminal = record["terminal_hash"]
    return Run(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        RunState(str(record["state"])),
        str(record["current_node_id"]),
        int(record["state_version"]),
        int(record["last_event_sequence"]),
        None if terminal is None else Sha256Hash(str(terminal)),
        int(record["current_round_ordinal"]),
    )


def _agent_binding_select() -> sa.Select[Any]:
    """The one shape a run's agent bindings are read in, for one run or many.

    The caller adds the `run_id` predicate: `load_run` reads one run, a page read
    reads every run it lists in the same statement.
    """

    return (
        sa.select(
            run_agent_bindings.c.run_id,
            run_agent_bindings.c.revision_hash.label("run_revision_hash"),
            run_agent_bindings.c.binding_set_hash,
            run_agent_bindings.c.role,
            run_agent_bindings.c.agent_configuration_revision_hash,
            agent_configuration_revisions.c.revision_hash.label(
                "configuration_revision_hash"
            ),
            agent_configuration_revisions.c.model,
            agent_configuration_revisions.c.auth_profile_revision_hash,
            agent_configuration_revisions.c.executor_revision,
            agent_configuration_revisions.c.revision_format_version,
            agent_configuration_revisions.c.requested_capability,
            auth_profile_revisions.c.revision_hash.label("auth_revision_hash"),
            auth_profile_revisions.c.profile_id,
            auth_profile_revisions.c.revision_number,
            auth_profile_revisions.c.provider_id,
            auth_profile_revisions.c.auth_mode,
        )
        .join(
            agent_configuration_revisions,
            agent_configuration_revisions.c.revision_hash
            == run_agent_bindings.c.agent_configuration_revision_hash,
        )
        .join(
            auth_profile_revisions,
            auth_profile_revisions.c.revision_hash
            == agent_configuration_revisions.c.auth_profile_revision_hash,
        )
        .order_by(
            run_agent_bindings.c.run_id,
            sa.cast(run_agent_bindings.c.role, sa.LargeBinary()),
        )
    )


def run_from_record_with_bindings(session: Any, record: Mapping[Any, Any]) -> AnyRun:
    """One run, reading the agent bindings it stands on."""

    if _binds_agent_roles(record):
        run_id = str(record["run_id"])
        rows = tuple(
            session.execute(
                _agent_binding_select().where(run_agent_bindings.c.run_id == run_id)
            ).mappings()
        )
    else:
        rows = ()
    return run_from_record_and_binding_rows(record, rows)


def _binds_agent_roles(record: Mapping[Any, Any]) -> bool:
    """Whether this row's format reads agent bindings at all."""

    return WorkflowFormatVersion(int(record["workflow_format_version"])) is not (
        WorkflowFormatVersion.V1
    )


def runs_from_records_with_bindings(
    session: Any, records: Sequence[Mapping[Any, Any]]
) -> tuple[AnyRun, ...]:
    """Every run of a page, reading all their agent bindings in one statement.

    A page that asked per run would issue one statement per listed run, which is
    the read a listing cannot afford: the same rows arrive here grouped instead.
    """

    binding_run_ids = tuple(
        str(record["run_id"]) for record in records if _binds_agent_roles(record)
    )
    rows_by_run: dict[str, list[Mapping[Any, Any]]] = {}
    if binding_run_ids:
        for row in session.execute(
            _agent_binding_select().where(
                run_agent_bindings.c.run_id.in_(binding_run_ids)
            )
        ).mappings():
            rows_by_run.setdefault(str(row["run_id"]), []).append(row)
    return tuple(
        run_from_record_and_binding_rows(
            record, tuple(rows_by_run.get(str(record["run_id"]), ()))
        )
        for record in records
    )


def run_from_record_and_binding_rows(
    record: Mapping[Any, Any], rows: Sequence[Mapping[Any, Any]]
) -> AnyRun:
    """One run built from its own row and the binding rows already read for it."""

    version = WorkflowFormatVersion(int(record["workflow_format_version"]))
    if version is WorkflowFormatVersion.V1:
        return run_from_record(record)
    # A V3 run binds its agent roles exactly as a V2 run does, so the rows below
    # are read the same way; what differs about V3 lives in the graph, not here.
    if (
        version not in (WorkflowFormatVersion.V2, WorkflowFormatVersion.V3)
        or record["agent_binding_set_hash"] is None
    ):
        raise RunTransitionConflict("run format version and binding set disagree")
    run_id = RunId(str(record["run_id"]))
    revision_hash = WorkflowRevisionHash(str(record["revision_hash"]))
    binding_set_hash = AgentBindingSetHash(str(record["agent_binding_set_hash"]))
    resolved: list[ResolvedAgentBinding] = []
    for row in rows:
        if (
            str(row["run_revision_hash"]) != revision_hash.value
            or str(row["binding_set_hash"]) != binding_set_hash.value
        ):
            raise RunTransitionConflict("run agent binding row disagrees with run")
        configuration = agent_configuration_from_record(
            {
                "revision_hash": row["configuration_revision_hash"],
                "model": row["model"],
                "auth_profile_revision_hash": row["auth_profile_revision_hash"],
                "executor_revision": row["executor_revision"],
                "revision_format_version": row["revision_format_version"],
                "requested_capability": row["requested_capability"],
            }
        )
        auth = auth_profile_from_record(
            {
                "revision_hash": row["auth_revision_hash"],
                "profile_id": row["profile_id"],
                "revision_number": row["revision_number"],
                "provider_id": row["provider_id"],
                "auth_mode": row["auth_mode"],
            }
        )
        if (
            str(row["agent_configuration_revision_hash"])
            != configuration.revision_hash.value
        ):
            raise RunTransitionConflict("run agent configuration binding disagrees")
        resolved.append(
            ResolvedAgentBinding(AgentRole(str(row["role"])), configuration, auth)
        )
    terminal = record["terminal_hash"]
    # A V3 row is read back as a V3 run, not as a V2 one wearing a new number:
    # the two are different truths, and a shared shape would mean every V2 reader
    # had silently been reading V3 rows all along.
    head = (
        run_id,
        revision_hash,
        binding_set_hash,
        tuple(resolved),
        RunState(str(record["state"])),
        str(record["current_node_id"]),
        int(record["state_version"]),
        int(record["last_event_sequence"]),
    )
    terminal_hash = None if terminal is None else Sha256Hash(str(terminal))
    round_ordinal = int(record["current_round_ordinal"])
    if version is WorkflowFormatVersion.V2:
        return RunV2(*head, terminal_hash, round_ordinal)
    configuration = record["run_configuration_revision_hash"]
    if configuration is None:
        raise RunTransitionConflict(
            "a format-3 run row carries no run configuration revision"
        )
    return RunV3(
        *head,
        RunConfigurationRevisionHash(str(configuration)),
        terminal_hash,
        round_ordinal,
    )


def load_run(session: Any, run_id: RunId) -> AnyRun:
    record = (
        session.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("run does not exist")
    run = run_from_record_with_bindings(session, record)
    graph = load_graph(session, run.revision_hash)
    validate_run_graph_binding(run, graph)
    return run


def validate_run_graph_binding(run: AnyRun, graph: AnyWorkflowDocument) -> None:
    # Every graph is V3, which binds roles and is carried by a bound run shape;
    # only the bare V1 run shape has no bindings.
    bound_graph = isinstance(graph, WorkflowGraphV3)
    bound_run = isinstance(run, (RunV2, RunV3))
    if bound_run != bound_graph:
        raise RunTransitionConflict("run version differs from workflow graph version")
    if isinstance(run, RunV3) != isinstance(graph, WorkflowGraphV3):
        raise RunTransitionConflict("run format differs from workflow graph format")
    if isinstance(run, (RunV2, RunV3)):
        expected_roles = {
            node.role for node in graph.nodes if isinstance(node, AgentNodeV3)
        }
        if expected_roles != {binding.role.value for binding in run.agent_bindings}:
            raise RunTransitionConflict("run agent roles differ from workflow graph")
    try:
        node = graph.node(run.current_node_id)
    except KeyError as error:
        raise RunTransitionConflict(
            "run current node is absent from its workflow graph"
        ) from error
    if run.state is RunState.WAITING_INPUT and not isinstance(node, WaitNodeV3):
        raise RunTransitionConflict("WAITING_INPUT must name a Wait node")
    if run.state is RunState.WAITING_RECONCILIATION and not isinstance(
        node, (ActionNodeV3, AgentNodeV3)
    ):
        raise RunTransitionConflict(
            "WAITING_RECONCILIATION must name an effect-owning node"
        )
    if run.state is RunState.COMPLETED and not is_sink_node(graph, run.current_node_id):
        raise RunTransitionConflict("COMPLETED must name the run's sink node")
    _require_a_round_the_graph_declares(run, graph)


def _require_a_round_the_graph_declares(
    run: AnyRun, graph: AnyWorkflowDocument
) -> None:
    """Hold the round the run stands in to what its own document permits.

    Two ways a stored round could be a lie, and both are read here rather than
    trusted: a node no loop repeats standing anywhere but in the first round,
    and a looped node standing past the bound its author declared. Either would
    mean a durable row naming an execution the document cannot produce.
    """
    if not isinstance(graph, WorkflowGraphV3):
        if run.current_round_ordinal != FIRST_ROUND_ORDINAL:
            raise RunTransitionConflict("only a format-3 document declares a loop")
        return
    loop = graph.loop_of(run.current_node_id)
    if loop is None:
        if run.current_round_ordinal != FIRST_ROUND_ORDINAL:
            raise RunTransitionConflict("a node no loop repeats runs in one round")
        return
    if run.current_round_ordinal > loop.maximum_rounds:
        raise RunTransitionConflict("run stands past the loop's declared bound")


def event_from_record(record: Mapping[Any, Any]) -> RunEvent:
    logical_key = record["receipt_logical_key"]
    result_hash = record["receipt_result_hash"]
    attempt_binding = _event_attempt_binding_from_record(record)
    raw_wait_answer_actor = record["wait_answer_actor"]
    event = RunEvent(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        int(record["event_sequence"]),
        str(record["node_id"]),
        NodeExecutionId(str(record["node_execution_id"])),
        RunEventKind(str(record["event_kind"])),
        bytes(record["payload"]),
        None if logical_key is None else LogicalEffectKey(str(logical_key)),
        None if result_hash is None else Sha256Hash(str(result_hash)),
        attempt_binding,
        None
        if record["agent_receipt_hash"] is None
        else AgentReceiptHash(str(record["agent_receipt_hash"])),
        int(record["round_ordinal"]),
        (
            None
            if raw_wait_answer_actor is None
            else WaitAnswerActor(str(raw_wait_answer_actor))
        ),
    )
    if event.payload_hash.value != record["payload_hash"]:
        raise RunTransitionConflict("durable event payload hash disagrees")
    if event.event_hash.value != record["event_hash"]:
        raise RunTransitionConflict("durable event hash disagrees")
    return event


def _event_attempt_binding_from_record(
    record: Mapping[Any, Any],
) -> RunEventAttemptBinding | None:
    attempt_id = record["agent_attempt_id"]
    attempt_ordinal = record["attempt_ordinal"]
    command_id = record["cancellation_command_id"]
    replacement = record["replacement"]
    disposition = record["cancellation_disposition"]
    replacement_attempt_id = record["replacement_attempt_id"]
    cancellation_values = (
        command_id,
        replacement,
        disposition,
        replacement_attempt_id,
    )
    try:
        if attempt_id is None:
            if attempt_ordinal is not None or any(
                value is not None for value in cancellation_values
            ):
                raise ValueError("attempt columns disagree")
            return None
        typed_attempt_id = AgentAttemptId(str(attempt_id))
        if attempt_ordinal is None:
            raise ValueError("attempt ordinal is absent")
        ordinal = int(attempt_ordinal)
        if command_id is None:
            if any(value is not None for value in cancellation_values[1:]):
                raise ValueError("cancellation columns disagree")
            return RunEventAgentAttemptBinding(typed_attempt_id, ordinal)
        if replacement is None:
            raise ValueError("cancellation replacement is absent")
        return RunEventCancellationBinding(
            typed_attempt_id,
            ordinal,
            AgentAttemptReplacement(str(replacement)),
            str(command_id),
            (
                None
                if disposition is None
                else AgentAttemptCancellationDisposition(str(disposition))
            ),
            (
                None
                if replacement_attempt_id is None
                else AgentAttemptId(str(replacement_attempt_id))
            ),
        )
    except (TypeError, ValueError) as error:
        raise RunTransitionConflict(
            "durable event attempt binding is not canonical"
        ) from error


def _existing_event(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    event_kind: RunEventKind,
    payload: bytes,
    receipt_logical_key: LogicalEffectKey | None,
    receipt_result_hash: Sha256Hash | None,
    agent_attempt_id: AgentAttemptId | None = None,
    agent_receipt_hash: AgentReceiptHash | None = None,
    wait_answer_actor: WaitAnswerActor | None = None,
) -> TransitionSnapshot | None:
    # The round takes part in the lookup, not only in the comparison: a looped
    # node writes one event of this kind per round, and a retry means "the same
    # round again" rather than "some round of this node".
    record = (
        session.execute(
            sa.select(run_events).where(
                run_events.c.run_id == run_id.value,
                run_events.c.revision_hash == revision_hash.value,
                run_events.c.node_id == node_id,
                run_events.c.round_ordinal == round_ordinal,
                run_events.c.event_kind == event_kind.value,
                (
                    run_events.c.agent_attempt_id.is_(None)
                    if agent_attempt_id is None
                    else run_events.c.agent_attempt_id == agent_attempt_id.value
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    event = event_from_record(record)
    expected_execution = NodeExecutionId.for_node(
        run_id, revision_hash, node_id, round_ordinal
    )
    if (
        event.node_execution_id != expected_execution
        or event.payload != payload
        or event.receipt_logical_key != receipt_logical_key
        or event.receipt_result_hash != receipt_result_hash
        or event.agent_receipt_hash != agent_receipt_hash
        or event.wait_answer_actor != wait_answer_actor
    ):
        raise RunTransitionConflict("existing event differs from exact retry")
    current = load_run(session, run_id)
    if current.revision_hash != revision_hash:
        raise RunTransitionConflict("event retry names another run revision")
    return TransitionSnapshot(
        current.run_id,
        current.revision_hash,
        current.current_node_id,
        current.state,
        current.state_version,
        current.last_event_sequence,
        event,
        current.current_round_ordinal,
    )


def _insert_event(session: Any, event: RunEvent, at: RecordedAt | None = None) -> None:
    attempt_binding = event.attempt_binding
    cancellation_binding = (
        attempt_binding
        if isinstance(attempt_binding, RunEventCancellationBinding)
        else None
    )
    insertion = run_events.insert().values(
        run_id=event.run_id.value,
        revision_hash=event.revision_hash.value,
        event_sequence=event.event_sequence,
        node_id=event.node_id,
        node_execution_id=event.node_execution_id.value,
        event_kind=event.event_kind.value,
        payload=event.payload,
        payload_hash=event.payload_hash.value,
        receipt_logical_key=(
            None
            if event.receipt_logical_key is None
            else event.receipt_logical_key.value
        ),
        receipt_result_hash=(
            None
            if event.receipt_result_hash is None
            else event.receipt_result_hash.value
        ),
        event_hash=event.event_hash.value,
        agent_attempt_id=(
            None if attempt_binding is None else attempt_binding.attempt_id.value
        ),
        attempt_ordinal=(
            None if attempt_binding is None else attempt_binding.attempt_ordinal
        ),
        cancellation_command_id=(
            None if cancellation_binding is None else cancellation_binding.command_id
        ),
        replacement=(
            None
            if cancellation_binding is None
            else cancellation_binding.replacement.value
        ),
        cancellation_disposition=(
            None
            if cancellation_binding is None or cancellation_binding.disposition is None
            else cancellation_binding.disposition.value
        ),
        replacement_attempt_id=(
            None
            if cancellation_binding is None
            or cancellation_binding.replacement_attempt_id is None
            else cancellation_binding.replacement_attempt_id.value
        ),
        agent_receipt_hash=(
            None if event.agent_receipt_hash is None else event.agent_receipt_hash.value
        ),
        round_ordinal=event.round_ordinal,
        wait_answer_actor=(
            None if event.wait_answer_actor is None else event.wait_answer_actor.value
        ),
    )
    # The driver's own integrity error carries the bound parameters, and the
    # payload among them is a `memoryview` the durable step boundary above
    # cannot serialise. An error that cannot be recorded is one the run never
    # hears about: the node workflow would stand STARTED with nothing left to
    # move it. The refusal is therefore restated in words -- the event it names
    # and the store's own reason for it -- which cross that boundary.
    try:
        session.execute(insertion)
    except IntegrityError as error:
        raise RunTransitionConflict(
            f"the event log refused {event.event_kind.value} of node "
            f"{event.node_id} round {event.round_ordinal}: {error.orig}"
        ) from error
    record_event_instant(session, event.run_id.value, event.event_sequence, at=at)


def _refuse_unfit_target(
    graph: AnyWorkflowDocument, target_state: RunState, node_id: str, terminal: bool
) -> None:
    node = graph.node(node_id)
    if target_state is RunState.WAITING_INPUT and not isinstance(node, WaitNodeV3):
        raise RunTransitionConflict("WAITING_INPUT target is not a Wait node")
    if target_state is RunState.WAITING_RECONCILIATION and not isinstance(
        node, (ActionNodeV3, AgentNodeV3)
    ):
        raise RunTransitionConflict(
            "WAITING_RECONCILIATION target is not an effect-owning node"
        )
    # Which words end a run has one owner, and CANCELLED is one of them: a run
    # resting at a pause ends here, under its own attestation, rather than
    # standing WAITING_INPUT forever because no attempt existed to stop.
    if terminal != (target_state in TERMINAL_RUN_STATES):
        raise RunTransitionConflict("terminal transition shape disagrees")
    if (
        terminal
        and target_state is RunState.COMPLETED
        and not is_sink_node(graph, node_id)
    ):
        raise RunTransitionConflict("terminal transition must finish the run's sink")


def _event_hashes(session: Any, run_id: RunId) -> tuple[Sha256Hash, ...]:
    """Every event hash this run has written, in the order the terminal hash folds."""
    return tuple(
        Sha256Hash(str(value))
        for value in session.execute(
            sa.select(run_events.c.event_hash)
            .where(run_events.c.run_id == run_id.value)
            .order_by(run_events.c.event_sequence)
        ).scalars()
    )


def _commit_event(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    event_kind: RunEventKind,
    payload: bytes,
    expected_state: RunState,
    target_state: RunState,
    target_node_id: str,
    receipt_logical_key: LogicalEffectKey | None = None,
    receipt_result_hash: Sha256Hash | None = None,
    terminal: bool = False,
    agent_attempt_id: AgentAttemptId | None = None,
    attempt_ordinal: int | None = None,
    agent_receipt_hash: AgentReceiptHash | None = None,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
    target_round_ordinal: int = FIRST_ROUND_ORDINAL,
    wait_answer_actor: WaitAnswerActor | None = None,
) -> TransitionSnapshot:
    existing = _existing_event(
        session,
        run_id,
        revision_hash,
        node_id,
        round_ordinal,
        event_kind,
        payload,
        receipt_logical_key,
        receipt_result_hash,
        agent_attempt_id,
        agent_receipt_hash,
        wait_answer_actor,
    )
    if existing is not None:
        return existing
    current = load_run(session, run_id)
    if (
        current.revision_hash != revision_hash
        or current.current_node_id != node_id
        or current.current_round_ordinal != round_ordinal
        or current.state is not expected_state
    ):
        raise RunTransitionConflict("run is not at the transition's exact source")
    graph = load_graph(session, revision_hash)
    _refuse_unfit_target(graph, target_state, target_node_id, terminal)
    instant = recorded_instant()
    sequence = current.last_event_sequence + 1
    if (agent_attempt_id is None) != (attempt_ordinal is None):
        raise RunTransitionConflict("agent event attempt binding is incomplete")
    attempt_binding = (
        None
        if agent_attempt_id is None or attempt_ordinal is None
        else RunEventAgentAttemptBinding(agent_attempt_id, attempt_ordinal)
    )
    event = RunEvent(
        run_id,
        revision_hash,
        sequence,
        node_id,
        NodeExecutionId.for_node(run_id, revision_hash, node_id, round_ordinal),
        event_kind,
        payload,
        receipt_logical_key,
        receipt_result_hash,
        attempt_binding,
        agent_receipt_hash=agent_receipt_hash,
        round_ordinal=round_ordinal,
        wait_answer_actor=wait_answer_actor,
    )
    terminal_hash: Sha256Hash | None = None
    if terminal:
        _insert_event(session, event, at=instant)
        terminal_hash = terminal_hash_for(revision_hash, _event_hashes(session, run_id))
    updated = session.execute(
        runs.update()
        .where(
            runs.c.run_id == run_id.value,
            runs.c.revision_hash == revision_hash.value,
            runs.c.current_node_id == node_id,
            runs.c.current_round_ordinal == round_ordinal,
            runs.c.state == expected_state.value,
            runs.c.state_version == current.state_version,
            runs.c.last_event_sequence == current.last_event_sequence,
        )
        .values(
            current_node_id=target_node_id,
            current_round_ordinal=target_round_ordinal,
            state=target_state.value,
            state_version=current.state_version + 1,
            last_event_sequence=sequence,
            terminal_hash=None if terminal_hash is None else terminal_hash.value,
        )
    )
    if updated.rowcount != 1:
        raise RunTransitionConflict("run transition lost its state/version CAS")
    if terminal:
        record_run_ended(session, run_id.value, at=instant)
    else:
        _insert_event(session, event, at=instant)
    return TransitionSnapshot(
        run_id,
        revision_hash,
        target_node_id,
        target_state,
        current.state_version + 1,
        sequence,
        event,
        target_round_ordinal,
    )


def commit_waiting_input(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
    question: str | None = None,
) -> TransitionSnapshot:
    """Rest this run at this node's pause, in the round it is already turning.

    The pause moves nowhere, so source and target round are the same one. An
    input-bearing V3 Wait carries the exact composed question in this event;
    the event's payload hash is its integrity check. Legacy and no-input pauses
    keep the exact empty payload they have always written.
    """
    payload = b"" if question is None else question.encode("utf-8")
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.WAITING_INPUT,
        payload,
        RunState.STARTED,
        RunState.WAITING_INPUT,
        node_id,
        round_ordinal=round_ordinal,
        target_round_ordinal=round_ordinal,
        wait_answer_actor=WaitAnswerActor.OPERATOR,
    )


def commit_wait_cancelled(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    command_id: str,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> TransitionSnapshot:
    """End a run resting at this pause, under the command that ordered it.

    The event is the cancellation's whole attestation: no attempt exists to
    stamp, so this is the last entry the run's terminal hash folds over, and
    `lift_started_run` -- which never invents an event -- is not the seam a
    resting pause can use anyway, because that lift only moves a STARTED run.

    The run stops where it stood. A cancelled pause reaches no successor, so
    source and target node and round are the one execution the operator's
    confirmation fenced on.
    """
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.WAIT_CANCELLED,
        command_id.encode("utf-8"),
        RunState.WAITING_INPUT,
        RunState.CANCELLED,
        node_id,
        terminal=True,
        round_ordinal=round_ordinal,
        target_round_ordinal=round_ordinal,
    )


def commit_reconciliation_required(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    request: bytes,
    current_round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> TransitionSnapshot:
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED,
        request,
        RunState.STARTED,
        RunState.WAITING_RECONCILIATION,
        node_id,
        round_ordinal=current_round_ordinal,
        target_round_ordinal=current_round_ordinal,
    )


def lift_started_run(
    connection: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    expected_state_version: int,
    expected_last_event_sequence: int,
    target_state: RunState,
) -> bool:
    """Close a STARTED run at its exact current head, without writing an event.

    The lift itself never invents an event: whatever left the run with nowhere
    to go -- a node's own terminal receipt, a cancellation's attestation --
    already wrote the last entry the run's terminal hash folds over. This is
    the one owner both `uncontinuable_runs`' serve-start inventory and an
    attempt store's own in-transaction cancel lift call (#439 Bauplan P3):
    the target word differs by caller and by why the run stopped, but the
    CAS and the terminal hash it stamps do not.

    A run whose log is still empty is lifted like any other, over the empty
    fold. Its terminal hash then says exactly what happened: this revision, and
    nothing after it. Refusing that instead left a run whose carrier died before
    writing its first event `STARTED` for as long as the store existed, which is
    the one answer that is certainly wrong (#636) -- and the hash is no weaker
    for it, because `terminal_hash_for` frames the revision either way, so an
    empty fold is a distinct value rather than an absent one.

    `False` still means the caller's snapshot no longer names the run's live row.
    """
    terminal_hash = terminal_hash_for(revision_hash, _event_hashes(connection, run_id))
    updated = connection.execute(
        runs.update()
        .where(
            runs.c.run_id == run_id.value,
            runs.c.state == RunState.STARTED.value,
            runs.c.state_version == expected_state_version,
            runs.c.last_event_sequence == expected_last_event_sequence,
        )
        .values(
            state=target_state.value,
            state_version=expected_state_version + 1,
            terminal_hash=terminal_hash.value,
        )
    )
    if updated.rowcount == 1:
        record_run_ended(connection, run_id.value)
    return updated.rowcount == 1


def commit_reconciliation_resolved(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    logical_key: LogicalEffectKey,
    result: bytes,
    result_hash: Sha256Hash,
    current_round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> TransitionSnapshot:
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.ACTION_RECONCILIATION_RESOLVED,
        result,
        RunState.WAITING_RECONCILIATION,
        RunState.STARTED,
        node_id,
        logical_key,
        result_hash,
        round_ordinal=current_round_ordinal,
        target_round_ordinal=current_round_ordinal,
    )
