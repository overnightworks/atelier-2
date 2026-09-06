from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, assert_never

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.effect_store import (
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.names import ANSWER_WORKFLOW_NAME, QUEUE_NAME
from atelier2.adapters.dbos.node_records import (
    keep_node_receipt,
    node_receipt_from_record,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    _commit_event,
    event_from_record,
    graph_from_document,
    load_graph,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.schema import (
    context_packages_v3,
    effect_intents,
    effect_receipts,
    node_artifacts_v3,
    node_execution_requests_v3,
    node_receipts_v3,
    published_revisions,
    run_events,
    run_fork_reused_nodes,
    run_forks,
    run_inputs_v3,
    runs,
    wait_answers,
    workflow_revisions,
)
from atelier2.adapters.dbos.workflow_ids import answer_workflow_id_for
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentBindingSetHash,
    AgentConfigurationRevisionHash,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentOutputHash,
    AgentReceiptHash,
    AgentReceiptV2,
    AgentRole,
    AuthMode,
    AuthProfileRevisionHash,
    ProviderId,
)
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import (
    LegacyWaitAnswerAttribution,
    NodeExecutionId,
    RunEvent,
    RunEventKind,
    SubmitWaitAnswerRequest,
    TransitionSnapshot,
    WaitAnswer,
    WaitAnswerActor,
    WaitAnswerAttributionKind,
    WaitAnswerSnapshot,
    WaitAnswerState,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    DeclaredContextPackage,
    DeliveredOutput,
    NodeArtifact,
    NodeReceiptReason,
    PersistedReceiptDisposition,
    RunInput,
    node_receipt_reason,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import AnyRun, RunV3
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.schemas_v3 import (
    InstanceRefused,
    SchemaAccepted,
    SchemaRefused,
    read_authored_instance_document,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantCapability,
    ToolRedemptionReceipt,
)
from atelier2.contracts.workflows import (
    RunCompletes,
    RunContinues,
    completion_after_node,
    producing_round,
)
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocument,
    GraphInputSource,
    LoopVerdictNotRead,
    NodeOutput,
    NodeOutputSource,
    WaitNodeV3,
    WorkflowGraphV3,
    WorkflowNodeV3,
)
from atelier2.ports.durable_runs import (
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerNotAdmitted,
    DurableAnswerResult,
    DurableAnswerRevisionConflict,
    DurableAnswerRoundAnswered,
    DurableAnswerRunMissing,
    DurableAnswerStale,
    DurableAnswerStateConflict,
    DurableStateCorrupt,
    DurableWriteUnavailable,
)


class AgentReceiptConflict(RunTransitionConflict):
    """One stable node execution contradicts its durable agent receipt."""


class ToolRedemptionConflict(RunTransitionConflict):
    """One stable node execution contradicts its durable tool redemption."""


def load_run_inputs(
    session: Any, run_id: RunId, node: AgentNodeV3 | WaitNodeV3
) -> tuple[RunInput, ...]:
    """The orders this node declared it reads, as the start stored them.

    A node is handed what it asked for and not everything the run carries: a run
    may be started with orders several nodes divide between them, and a node that
    received one it never named would be told something its author did not ask
    for -- and would carry it in its durable request or question identity.

    The start refused the run unless every order it declares was supplied, so an
    order named here and absent from the store is a store that disagrees with the
    run it holds, not an input somebody forgot.

    This reads exactly the columns `run_inputs_v3` carries -- no join against
    `published_revisions` -- because #1091 retired the one reader that needed a
    second, resolved fact about the order's schema (whether its top level
    declared `type: string`, for composition). The start (`starter.py`)
    already resolved and validated that same schema revision before writing
    this row, and a schema revision is immutable once published, so trusting
    this durable pin here re-reads no invariant the write did not already
    settle.
    """
    read = {
        entry.source.graph_input
        for entry in node.inputs
        if isinstance(entry.source, GraphInputSource)
    }
    if not read:
        return ()
    stored = {
        str(record.name): RunInput(
            str(record.name),
            PublishedRevisionHash(str(record.schema_revision_hash)),
            bytes(record.value),
        )
        for record in session.execute(
            sa.select(run_inputs_v3).where(run_inputs_v3.c.run_id == run_id.value)
        ).all()
    }
    missing = sorted(read - stored.keys())
    if missing:
        raise RunTransitionConflict(
            f"the run carries no order named {missing[0]!r}, which this node reads"
        )
    return tuple(stored[name] for name in sorted(read))


def load_run_orders(
    session: Any, run_ids: Sequence[str]
) -> dict[str, tuple[RunInput, ...]]:
    """Every order each of these runs was started with, one query for the page.

    Unlike `load_run_inputs`, this answers a run's own purpose -- every order the
    start stored -- rather than the subset one node declared it reads. A run
    absent from the returned mapping carries none: `run_inputs_v3` has no row for
    a run started with no orders, which is a run's own honest answer and not a
    gap this reads around.
    """
    if not run_ids:
        return {}
    by_run: dict[str, list[RunInput]] = {run_id: [] for run_id in run_ids}
    for record in session.execute(
        sa.select(run_inputs_v3)
        .where(run_inputs_v3.c.run_id.in_(run_ids))
        .order_by(run_inputs_v3.c.run_id, run_inputs_v3.c.name)
    ).all():
        by_run[str(record.run_id)].append(
            RunInput(
                str(record.name),
                PublishedRevisionHash(str(record.schema_revision_hash)),
                bytes(record.value),
            )
        )
    return {run_id: tuple(orders) for run_id, orders in by_run.items()}


class NodeOutputNotWritten(RunTransitionConflict):
    """The node this one reads has not written its value yet.

    This is absence, not refusal. Nothing has judged anything: the predecessor
    simply has not run, or has not finished. It is its own class because a reader
    that cannot tell it from a refusal will report a waiting run as a stopped one
    -- and the driver still treats it as the conflict it is, because a driver
    reaching here has asked for a value the run does not have.
    """


class NodeOutputSchemaRefused(RunTransitionConflict):
    """A produced value is not what the schema its author pinned admits.

    This is the refusal: something judged, and said no. It carries the words of
    the schema owner that judged it, so a reader is told what the run was told.
    """


def bootstrap_node_for_snapshot(
    session: Any, run: AnyRun, graph: AnyWorkflowDocument
) -> str:
    """Validate the one pristine snapshot an ordinary start or fork may drive."""

    if (
        run.state is not RunState.STARTED
        or run.state_version != 0
        or run.last_event_sequence != 0
        or run.current_round_ordinal != FIRST_ROUND_ORDINAL
    ):
        raise RunTransitionConflict("bootstrap requires its exact new durable run")
    fork = (
        session.execute(
            sa.select(run_forks).where(run_forks.c.successor_run_id == run.run_id.value)
        )
        .mappings()
        .one_or_none()
    )
    if fork is None:
        entry = entry_node_of(graph)
        if run.current_node_id != entry:
            raise RunTransitionConflict("ordinary bootstrap requires the graph entry")
        return entry
    if not isinstance(run, RunV3) or not isinstance(graph, WorkflowGraphV3):
        raise RunTransitionConflict("only a V3 run may carry fork lineage")
    origin = (
        session.execute(
            sa.select(runs.c.terminal_hash, runs.c.revision_hash).where(
                runs.c.run_id == str(fork["origin_run_id"])
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        str(fork["workflow_revision_hash"]) != run.revision_hash.value
        or str(fork["run_configuration_revision_hash"])
        != run.run_configuration_revision_hash.value
        or str(fork["restart_from_node_id"]) != run.current_node_id
        or origin is None
        or str(origin["revision_hash"]) != run.revision_hash.value
        or str(origin["terminal_hash"]) != str(fork["origin_terminal_hash"])
    ):
        raise RunTransitionConflict("fork bootstrap lineage disagrees with its run")
    try:
        graph.node(run.current_node_id)
    except KeyError as error:
        raise RunTransitionConflict("fork bootstrap node left its graph") from error
    return run.current_node_id


def load_node_output_payload(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    graph: WorkflowGraphV3,
    producer_id: str,
    round_ordinal: int,
) -> bytes:
    """Read one producer value locally or through its validated immutable fork rail."""

    producer = graph.node(producer_id)
    execution_id = NodeExecutionId.for_node(
        run_id, revision_hash, producer_id, round_ordinal
    )
    local = (
        session.execute(
            sa.select(run_events).where(
                run_events.c.run_id == run_id.value,
                run_events.c.revision_hash == revision_hash.value,
                run_events.c.node_execution_id == execution_id.value,
                run_events.c.event_kind == event_carrying_the_output_of(producer).value,
            )
        )
        .mappings()
        .one_or_none()
    )
    if local is not None:
        return event_from_record(local).payload

    reference = (
        session.execute(
            sa.select(run_fork_reused_nodes).where(
                run_fork_reused_nodes.c.successor_run_id == run_id.value,
                run_fork_reused_nodes.c.node_id == producer_id,
                run_fork_reused_nodes.c.round_ordinal == round_ordinal,
            )
        )
        .mappings()
        .one_or_none()
    )
    if reference is None:
        raise NodeOutputNotWritten(
            f"node {producer_id!r} has written no output this node can read"
        )
    source_run_id = RunId(str(reference["source_run_id"]))
    source_revision = WorkflowRevisionHash(
        str(reference["source_workflow_revision_hash"])
    )
    source_execution = NodeExecutionId.for_node(
        source_run_id, source_revision, producer_id, round_ordinal
    )
    if source_execution.value != str(reference["source_node_execution_id"]):
        raise RunTransitionConflict("fork output reference execution disagrees")
    source_event_record = (
        session.execute(
            sa.select(run_events).where(
                run_events.c.event_hash == str(reference["source_event_hash"]),
                run_events.c.run_id == source_run_id.value,
                run_events.c.revision_hash == source_revision.value,
                run_events.c.node_execution_id == source_execution.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    receipt_record = (
        session.execute(
            sa.select(node_receipts_v3).where(
                node_receipts_v3.c.node_execution_id == source_execution.value,
                node_receipts_v3.c.receipt_hash
                == str(reference["source_receipt_hash"]),
            )
        )
        .mappings()
        .one_or_none()
    )
    request_record = (
        session.execute(
            sa.select(node_execution_requests_v3).where(
                node_execution_requests_v3.c.node_execution_id == source_execution.value
            )
        )
        .mappings()
        .one_or_none()
    )
    manifest = session.scalar(
        sa.select(context_packages_v3.c.manifest).where(
            context_packages_v3.c.package_hash
            == str(reference["source_declared_context_package_hash"])
        )
    )
    if (
        source_event_record is None
        or receipt_record is None
        or request_record is None
        or manifest is None
    ):
        raise RunTransitionConflict("fork output source evidence is incomplete")
    event = event_from_record(source_event_record)
    receipt = node_receipt_from_record(session, receipt_record)
    package = DeclaredContextPackage(bytes(manifest))
    if (
        event.event_kind != event_carrying_the_output_of(producer)
        or event.node_id != producer_id
        or event.round_ordinal != round_ordinal
        or receipt.disposition is not PersistedReceiptDisposition.SUCCEEDED
        or receipt.context_package_hash.value
        != str(reference["source_declared_context_package_hash"])
        or str(request_record["request_hash"]) != receipt.request_hash.value
        or str(request_record["context_package_hash"])
        != receipt.context_package_hash.value
        or package.package_hash != receipt.context_package_hash
    ):
        raise RunTransitionConflict("fork output source evidence disagrees")
    return event.payload


def event_carrying_the_output_of(node: WorkflowNodeV3) -> RunEventKind:
    """Which event's payload is this node's declared output.

    Where a value lives is a fact about the node that produced it, not about the
    node reading it: an Agent node's output is the payload its attempt completed
    with, and a Wait node's output is the answer a person gave. Both are the one
    value their author declared a schema for, both are hash-bound by the event
    that wrote them, and the reader asks the same question of either.

    Reading only the Agent's own event is what let a document declaring
    `from: {node: <a wait>, ...}` pass the executable admission and then die at
    the hand-off, because the answer was durable in an event nothing looked in.

    The remaining kinds declare no output an executable document may read -- an
    Action node's outputs are refused as an authored form, and no runtime reaches
    the other two -- so a source naming one cannot be reached from an admitted
    document. It is refused by name rather than answered with an empty read.
    """
    match node:
        case AgentNodeV3():
            return RunEventKind.AGENT_COMPLETED
        case WaitNodeV3():
            return RunEventKind.WAIT_ANSWERED
        case _:
            raise RunTransitionConflict(
                f"node {node.id!r} is of a kind that writes no output to read"
            )


def load_node_outputs(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    graph: AnyWorkflowDocument,
    node: AgentNodeV3 | WaitNodeV3,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> tuple[DeliveredOutput, ...]:
    """The work of earlier nodes this Agent or Wait reads, as they wrote it.

    The value is the producing node's own completion payload -- carried by
    whichever event finished that node, which `event_carrying_the_output_of`
    answers from the producer's kind -- and it is verified against the hash that
    event stored, exactly as the Action path has always verified an Agent output
    it consumes. A payload that no longer matches its hash is a store that
    disagrees with itself, and it refuses here rather than travelling into a job.

    It is then read against the schema the producing node's author pinned for
    that output, through the same door that first admitted it: an Agent's output
    is a produced JSON value, while a Wait's answer is authored text. A value
    written by an older build never passed that write, so the value that travels
    is judged where it travels rather than trusted for having been stored.

    A node that reads nothing gets nothing: no query runs, and the composition
    is the authored instruction alone.

    Which round wrote the value is read from the graph, never guessed.
    `producing_round` answers: a predecessor the edges order wrote in the round
    now turning, a loop-mate the edges cannot name wrote in the previous round,
    and a producer no loop repeats wrote once. Round one of a previous-round
    edge delivers nothing — the source has not written yet, and that absence is
    not a missing write. The query names the producing execution rather than the
    producing node -- a node id alone would match every round at once, and a
    store that has several answers to one question is a store that cannot
    answer it.
    """
    read = tuple(
        entry.source
        for entry in node.inputs
        if isinstance(entry.source, NodeOutputSource)
    )
    if not read:
        return ()
    if not isinstance(graph, WorkflowGraphV3):
        raise RunTransitionConflict("a V3 Agent or Wait node belongs to a V3 document")
    delivered: list[DeliveredOutput] = []
    for source in sorted(read, key=lambda named: (named.node, named.output)):
        written_in = producing_round(graph, node.id, source.node, round_ordinal)
        if written_in is None:
            continue
        producer = graph.node(source.node)
        payload = load_node_output_payload(
            session,
            run_id,
            revision_hash,
            graph,
            source.node,
            written_in,
        )
        declared = next(
            output for output in producer.outputs if output.name == source.output
        )
        if isinstance(producer, WaitNodeV3):
            refusal = why_a_wait_node_does_not_admit_an_answer(
                session, producer, payload
            )
            if refusal is not None:
                raise NodeOutputSchemaRefused(
                    f"node {source.node!r} carried an answer its own schema "
                    f"refuses: {refusal}"
                )
        else:
            refuse_an_output_its_schema_does_not_admit(
                session, source.node, declared, payload
            )
        delivered.append(DeliveredOutput(source.node, source.output, payload))
    return tuple(delivered)


def load_kept_value(session: Any, node_execution_id: NodeExecutionId) -> bytes:
    """The exact value one finished node execution kept, as its artifact holds it.

    A driver that recovers after a round has already succeeded has to reach the
    same continuation that success reached, and a continuation a verdict steers
    is only recomputable from the answer that round gave. The artifact is where
    that answer durably lives, so it is read back rather than re-derived from
    anything that could have moved since.
    """
    value = session.scalar(
        sa.select(node_artifacts_v3.c.value).where(
            node_artifacts_v3.c.node_execution_id == node_execution_id.value
        )
    )
    if value is None:
        raise RunTransitionConflict(
            "a succeeded node execution kept no value its loop can read"
        )
    return bytes(value)


def refuse_an_output_its_schema_does_not_admit(
    session: Any,
    node_id: str,
    declared: NodeOutput,
    payload: bytes,
) -> None:
    """Read one produced value against the schema its own author pinned.

    This is the one place a produced value meets its declared schema, and both
    moments a value has ask it: the success write, before the node may be said to
    have succeeded, and the hand-off, before the value reaches the node that reads
    it. One owner means one profile -- a second reading could admit what the first
    refuses, which is exactly how an unenforced contract looks from outside.

    Authority is the provider-neutral core's: what judges the bytes is the schema
    profile owner, over the exact decoded bytes, whatever an adapter's own
    structured-output help may have promised.

    The pinned revision is read from the durable document rather than from the
    frozen resolution matrix, and that is not a second resolver: the reference is
    immutable inside the revision this run is bound to, and the start already
    refused the document if that revision was not a schema this product can
    enforce. What is added here is the one thing the start could not do -- reading
    a value that did not exist yet.
    """
    refusal = why_a_value_its_declared_schema_refuses(
        session, node_id, declared, payload
    )
    if refusal is not None:
        raise NodeOutputSchemaRefused(
            f"node {node_id!r} produced an output its own schema refuses: {refusal}"
        )


def load_published_schema_document(session: Any, revision: str) -> bytes | None:
    """The exact published schema document this revision stores, or nothing.

    This is the one read the output seam and the provider flag share. Callers
    do not parse or reserialize: the stored bytes are the schema.
    """
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.SCHEMA.value,
            published_revisions.c.revision_hash == revision,
        )
    )
    return None if document is None else bytes(document)


def _pinned_schema_or_conflict(
    session: Any, node_id: str, declared: NodeOutput
) -> SchemaAccepted:
    """The schema `declared`'s pinned revision reads as, or a raised transition conflict.

    Both a produced value (`why_a_value_its_declared_schema_refuses`) and an
    authored one (`why_a_wait_node_does_not_admit_an_answer`) are judged
    against the exact schema the run's own document pinned for this output --
    this is the one place either reads it, so the two judgements can never
    disagree about which schema they mean. A store that cannot answer for the
    schema it froze is neither's fault, and still raises -- that is the store
    disagreeing with itself.
    """
    document = load_published_schema_document(
        session, declared.schema_reference.revision
    )
    if document is None:
        raise RunTransitionConflict(
            f"the schema node {node_id!r} pinned for output "
            f"{declared.name!r} is absent from the store"
        )
    schema = read_schema_document(document)
    if isinstance(schema, SchemaRefused):
        raise RunTransitionConflict(
            f"the schema node {node_id!r} pinned for output "
            f"{declared.name!r} is not one: {schema}"
        )
    return schema


def why_a_value_its_declared_schema_refuses(
    session: Any,
    node_id: str,
    declared: NodeOutput,
    payload: bytes,
) -> str | None:
    """The schema owner's own words against this produced value, or nothing where it admits it.

    A refusal is answered rather than raised because this seam's caller owes
    its own caller a different vocabulary: a node that produced bad bytes is a
    transition conflict, not an error of the run's own author.

    `payload` is judged as a produced value (`read_instance_document`,
    always JSON): it comes back from a node whose own execution already
    promised the schema it would honour, so a `"string"`-typed schema still
    demands a JSON-encoded instance here -- unlike an authored value's own
    door, `why_a_wait_node_does_not_admit_an_answer`.

    The byte bound belongs to the route the value arrived by
    (`schemas_v3.py`'s `read_instance_document` docstring): a produced output
    arrives through the provider frame, whose route bound is
    `MAXIMUM_AGENT_OUTPUT_BYTES_V2`, not `read_instance_document`'s smaller
    inline-order default. This is the same bound the write door applies
    (`agent_attempt_store.py`'s `_declared_output_schema_refusal`) to the same
    payload, so a report the write admits is not later refused here under a
    narrower bound (#1078 edge 1).
    """
    schema = _pinned_schema_or_conflict(session, node_id, declared)
    verdict = read_instance_document(
        payload, schema, maximum_bytes=MAXIMUM_AGENT_OUTPUT_BYTES_V2
    )
    return str(verdict) if isinstance(verdict, InstanceRefused) else None


def entry_node_of(graph: AnyWorkflowDocument) -> str:
    """Where a run of this document begins.

    A graph derives its entry set from the nodes that depend on nothing. Only
    a single-entry document starts today — a fan-out start needs the ready set
    ADR 0006 hands the scheduler, and refusing it here is what keeps this head
    from implying one.
    """
    entry = graph.entry_node_ids
    if len(entry) != 1:
        raise RunTransitionConflict(
            f"a run starts at exactly one entry node, not {len(entry)}"
        )
    return entry[0]


def _agent_receipt_v2_values(receipt: AgentReceiptV2) -> dict[str, object]:
    return {
        "node_execution_id": receipt.node_execution_id.value,
        "request_hash": receipt.request_hash.value,
        "run_id": receipt.run_id.value,
        "workflow_revision_hash": receipt.workflow_revision_hash.value,
        "node_id": receipt.node_id,
        "role": receipt.role.value,
        "binding_set_hash": receipt.binding_set_hash.value,
        "agent_configuration_revision_hash": (
            receipt.agent_configuration_revision_hash.value
        ),
        "auth_profile_revision_hash": receipt.auth_profile_revision_hash.value,
        "profile_id": receipt.profile_id,
        "revision_number": receipt.revision_number,
        "provider_id": receipt.provider_id.value,
        "auth_mode": receipt.auth_mode.value,
        "model": receipt.model,
        "executor_revision": receipt.executor_revision.value,
        "executor_operational_identity": (receipt.executor_operational_identity.value),
        "output_bytes": receipt.output_bytes,
        "output_hash": receipt.output_hash.value,
        "receipt_hash": receipt.receipt_hash.value,
        "round_ordinal": receipt.round_ordinal,
    }


def _agent_receipt_v2_from_record(record: Mapping[Any, Any]) -> AgentReceiptV2:
    try:
        return AgentReceiptV2(
            AgentExecutionRequestHash(str(record["request_hash"])),
            NodeExecutionId(str(record["node_execution_id"])),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            str(record["node_id"]),
            AgentRole(str(record["role"])),
            AgentBindingSetHash(str(record["binding_set_hash"])),
            AgentConfigurationRevisionHash(
                str(record["agent_configuration_revision_hash"])
            ),
            AuthProfileRevisionHash(str(record["auth_profile_revision_hash"])),
            str(record["profile_id"]),
            int(record["revision_number"]),
            ProviderId(str(record["provider_id"])),
            AuthMode(str(record["auth_mode"])),
            str(record["model"]),
            AgentExecutorRevision(str(record["executor_revision"])),
            AgentExecutorOperationalIdentity(
                str(record["executor_operational_identity"])
            ),
            bytes(record["output_bytes"]),
            AgentOutputHash(str(record["output_hash"])),
            AgentReceiptHash(str(record["receipt_hash"])),
            int(record["round_ordinal"]),
        )
    except ValueError as error:
        raise AgentReceiptConflict(
            "durable V2 agent receipt hash binding disagrees"
        ) from error


def _tool_redemption_values(receipt: ToolRedemptionReceipt) -> dict[str, object]:
    """One redemption as its row, with the argv in this adapter's own encoding.

    The exact argv is a sequence and the row holds one value, so it travels as
    the JSON array this store reads back; nothing outside this adapter has a
    contract with that spelling, and the receipt hash is taken over the typed
    arguments rather than over the text.
    """
    return {
        "node_execution_id": receipt.node_execution_id.value,
        "run_id": receipt.run_id.value,
        "workflow_revision_hash": receipt.workflow_revision_hash.value,
        "node_id": receipt.node_id,
        "attempt_id": receipt.attempt_id.value,
        "tool_revision_hash": receipt.tool_revision_hash.value,
        "capability": receipt.capability.value,
        "command": json.dumps(list(receipt.command), ensure_ascii=False),
        "exit_code": receipt.exit_code,
        "standard_output_hash": receipt.standard_output_hash.value,
        "receipt_hash": receipt.receipt_hash.value,
    }


def _tool_redemption_from_record(record: Mapping[Any, Any]) -> ToolRedemptionReceipt:
    try:
        arguments = json.loads(str(record["command"]))
        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise ValueError("a durable redemption command is a list of arguments")
        return ToolRedemptionReceipt(
            NodeExecutionId(str(record["node_execution_id"])),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            str(record["node_id"]),
            AgentAttemptId(str(record["attempt_id"])),
            PublishedRevisionHash(str(record["tool_revision_hash"])),
            ToolGrantCapability(str(record["capability"])),
            tuple(str(argument) for argument in arguments),
            int(record["exit_code"]),
            Sha256Hash(str(record["standard_output_hash"])),
        )
    except ValueError as error:
        raise ToolRedemptionConflict(
            "durable tool redemption hash binding disagrees"
        ) from error


def commit_confirmed_effect(
    session: Any, logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
) -> TransitionSnapshot:
    intent_record = (
        session.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    receipt_record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if intent_record is None or receipt_record is None:
        raise RunTransitionConflict("confirmed effect requires its intent and receipt")
    intent = intent_snapshot_from_record(intent_record).intent
    receipt = receipt_from_record(receipt_record)
    run_id = intent.binding.run_id
    graph = load_graph(session, revision_hash)
    run_record = (
        session.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
        .mappings()
        .one_or_none()
    )
    if run_record is None:
        raise RunTransitionConflict("confirmed effect has no durable run")
    run = run_from_record_with_bindings(session, run_record)
    node = graph.node(run.current_node_id)
    if (
        run.revision_hash != revision_hash
        or run.state is not RunState.STARTED
        or not isinstance(node, (ActionNodeV3, AgentNodeV3))
        or logical_key
        != logical_effect_key_for_node(
            run_id, revision_hash, node.id, run.current_round_ordinal
        )
        or intent.binding.workflow_revision_hash != revision_hash
        or receipt.intent != intent
    ):
        raise RunTransitionConflict("logical effect key does not own current effect")
    if isinstance(node, ActionNodeV3):
        keep_node_receipt(
            session,
            NodeExecutionId.for_node(
                run_id, revision_hash, node.id, run.current_round_ordinal
            ),
            PersistedReceiptDisposition.SUCCEEDED,
            node_receipt_reason(NodeReceiptReason.EFFECT_CONFIRMED),
        )
    match completion_after_node(graph, node.id, run.current_round_ordinal):
        case RunContinues(successor, successor_round):
            target_state = RunState.STARTED
            target_node_id = successor
            target_round_ordinal = successor_round
            terminal = False
        case RunCompletes():
            target_state = RunState.COMPLETED
            target_node_id = node.id
            target_round_ordinal = run.current_round_ordinal
            terminal = True
        case _ as unreachable:
            assert_never(unreachable)
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node.id,
        RunEventKind.ACTION_COMPLETED,
        receipt.result.payload,
        RunState.STARTED,
        target_state,
        target_node_id,
        logical_key,
        receipt.result.payload_hash,
        terminal=terminal,
        round_ordinal=run.current_round_ordinal,
        target_round_ordinal=target_round_ordinal,
    )


def commit_action_completed(
    session: Any, logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
) -> TransitionSnapshot:
    """Commit a confirmed Action effect through the shared continuation."""
    return commit_confirmed_effect(session, logical_key, revision_hash)


def commit_wait_answered(session: Any, answer: WaitAnswer) -> TransitionSnapshot:
    record = _wait_answer_record(session, answer.node_execution_id)
    if record is None:
        raise RunTransitionConflict("answer workflow has no durable answer")
    durable = wait_answer_snapshot_from_record(record)
    if durable.answer != answer:
        raise RunTransitionConflict("answer workflow binding differs")
    graph = load_graph(session, answer.revision_hash)
    node = graph.node(answer.node_id)
    if isinstance(node, WaitNodeV3):
        declared = node.outputs[0]
        keep_node_receipt(
            session,
            answer.node_execution_id,
            PersistedReceiptDisposition.SUCCEEDED,
            node_receipt_reason(NodeReceiptReason.OUTPUT_ACCEPTED),
            NodeArtifact(
                answer.run_id,
                node.id,
                answer.node_execution_id,
                declared.name,
                PublishedRevisionHash(declared.schema_reference.revision),
                answer.answer_bytes,
            ),
        )
    # The answer is asked the same question every other completed node is asked --
    # is this the run's sink, and if not which heir did its author declare -- so a
    # Wait node standing last carries its own run to COMPLETED instead of handing
    # on to a successor no document names. It is asked in the answer's own round,
    # because a loop's last node hands back to the round's first one.
    match completion_after_node(graph, answer.node_id, answer.round_ordinal):
        case RunContinues(successor, target_round):
            target_state = RunState.STARTED
            target_node_id = successor
            target_round_ordinal = target_round
            terminal = False
        case RunCompletes():
            target_state = RunState.COMPLETED
            target_node_id = answer.node_id
            target_round_ordinal = answer.round_ordinal
            terminal = True
        case _ as unreachable:
            assert_never(unreachable)
    transition = _commit_event(
        session,
        answer.run_id,
        answer.revision_hash,
        answer.node_id,
        RunEventKind.WAIT_ANSWERED,
        answer.answer_bytes,
        RunState.WAITING_INPUT,
        target_state,
        target_node_id,
        terminal=terminal,
        round_ordinal=answer.round_ordinal,
        target_round_ordinal=target_round_ordinal,
    )
    if durable.state is WaitAnswerState.PENDING:
        updated = session.execute(
            wait_answers.update()
            .where(
                wait_answers.c.node_execution_id == answer.node_execution_id.value,
                wait_answers.c.state == WaitAnswerState.PENDING.value,
                wait_answers.c.state_version == 0,
            )
            .values(state=WaitAnswerState.APPLIED.value, state_version=1)
        )
        if updated.rowcount != 1:
            raise RunTransitionConflict("answer apply lost its state CAS")
    elif transition.event.event_kind is not RunEventKind.WAIT_ANSWERED:
        raise RunTransitionConflict("applied answer has no exact event")
    return transition


def commit_subworkflow_completed(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    result: int,
) -> TransitionSnapshot:
    payload = str(result).encode("ascii")
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.SUBWORKFLOW_COMPLETED,
        payload,
        RunState.STARTED,
        RunState.COMPLETED,
        node_id,
        terminal=True,
    )


def why_a_wait_node_does_not_admit_an_answer(
    session: Any, node: WaitNodeV3, answer_bytes: bytes
) -> str | None:
    """Why these bytes are no answer to this waiting node, or nothing where they are.

    A Wait node declares no answer type: it declares one output with a schema,
    and that schema is what judges the value -- the same schema owner every
    other value a run produces answers to, asked here about the one a person
    typed.

    `answer_bytes` is judged as an authored value (`read_authored_instance_document`),
    not a produced one: a person typing an answer owes no JSON-encoding
    promise a node's own execution would, so a `"string"`-typed schema reads
    the answer's raw text directly.

    It is answered here rather than in the use case that receives the submission,
    because which vocabulary applies is a fact about the node, and the node is
    only reachable from the document this store holds.
    """
    schema = _pinned_schema_or_conflict(session, node.id, node.outputs[0])
    verdict = read_authored_instance_document(answer_bytes, schema)
    return str(verdict) if isinstance(verdict, InstanceRefused) else None


def wait_answer_snapshot_from_record(record: Mapping[Any, Any]) -> WaitAnswerSnapshot:
    actor_value = record["actor"]
    attribution_kind = WaitAnswerAttributionKind(str(record["actor_attribution_kind"]))
    match attribution_kind:
        case WaitAnswerAttributionKind.RECORDED:
            if actor_value is None:
                raise RunTransitionConflict("recorded wait answer has no actor")
            actor = WaitAnswerActor(str(actor_value))
        case WaitAnswerAttributionKind.LEGACY_UNATTRIBUTED:
            if actor_value is not None:
                raise RunTransitionConflict(
                    "legacy wait answer carries an invented actor"
                )
            actor = LegacyWaitAnswerAttribution.UNATTRIBUTED
        case _ as unreachable:
            assert_never(unreachable)
    answer = WaitAnswer(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        str(record["node_id"]),
        NodeExecutionId(str(record["node_execution_id"])),
        actor,
        bytes(record["answer_bytes"]),
        int(record["round_ordinal"]),
    )
    if (
        answer.answer_hash.value != record["answer_hash"]
        or answer_workflow_id_for(answer.node_execution_id)
        != record["answer_workflow_id"]
    ):
        raise RunTransitionConflict("durable wait answer hashes or identity disagree")
    state = WaitAnswerState(str(record["state"]))
    state_version = int(record["state_version"])
    if (state, state_version) not in {
        (WaitAnswerState.PENDING, 0),
        (WaitAnswerState.APPLIED, 1),
    }:
        raise RunTransitionConflict("durable wait answer state and version disagree")
    return WaitAnswerSnapshot(answer, state, state_version)


class WaitAnswerStateCorrupt(RuntimeError):
    """Durable wait-answer rows contradict their one-execution identity."""


def _wait_answer_record(
    session: Any, node_execution_id: NodeExecutionId
) -> Mapping[Any, Any] | None:
    """The one stored answer of this exact execution, or nothing where none is.

    Every reader asks by execution identity because that is the row's own key:
    a node a loop turns holds one answer per round, and asking by node alone
    would answer with whichever round happens to come first.
    """
    records = tuple(
        session.execute(
            sa.select(wait_answers).where(
                wait_answers.c.node_execution_id == node_execution_id.value
            )
        ).mappings()
    )
    if len(records) > 1:
        raise WaitAnswerStateCorrupt("wait execution has duplicate durable answers")
    return records[0] if records else None


def load_wait_answer(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> WaitAnswerSnapshot:
    record = _wait_answer_record(
        session,
        NodeExecutionId.for_node(run_id, revision_hash, node_id, round_ordinal),
    )
    if record is None:
        raise RunTransitionConflict("wait answer does not exist")
    return wait_answer_snapshot_from_record(record)


def _events_for_wait_execution(
    session: Any, node_execution_id: NodeExecutionId
) -> tuple[RunEvent, ...]:
    return tuple(
        event_from_record(record)
        for record in session.execute(
            sa.select(run_events).where(
                run_events.c.node_execution_id == node_execution_id.value
            )
        ).mappings()
    )


def _wait_answer_binds_request(
    snapshot: WaitAnswerSnapshot, request: SubmitWaitAnswerRequest
) -> bool:
    answer = snapshot.answer
    return (
        answer.run_id == request.run_id
        and answer.revision_hash == request.revision_hash
        and answer.node_id == request.node_id
        and answer.node_execution_id == request.expected_node_execution_id
    )


def _wait_answer_binds_execution(
    snapshot: WaitAnswerSnapshot,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    node_execution_id: NodeExecutionId,
    round_ordinal: int,
) -> bool:
    answer = snapshot.answer
    return (
        answer.run_id == run_id
        and answer.revision_hash == revision_hash
        and answer.node_id == node_id
        and answer.node_execution_id == node_execution_id
        and answer.round_ordinal == round_ordinal
    )


def _applied_answer_matches_event(
    snapshot: WaitAnswerSnapshot,
    events: tuple[RunEvent, ...],
    request_actor: WaitAnswerActor,
) -> bool:
    answered = tuple(
        event for event in events if event.event_kind is RunEventKind.WAIT_ANSWERED
    )
    if len(answered) != 1:
        return False
    event = answered[0]
    answer = snapshot.answer
    return (
        snapshot.state is WaitAnswerState.APPLIED
        and event.run_id == answer.run_id
        and event.revision_hash == answer.revision_hash
        and event.node_id == answer.node_id
        and event.node_execution_id == answer.node_execution_id
        and event.round_ordinal == answer.round_ordinal
        and event.payload == answer.answer_bytes
        and event.payload_hash == answer.answer_hash
        and answer.actor == request_actor
    )


def _run_stands_on_its_head_event(
    graph: AnyWorkflowDocument, run: AnyRun, head_event: RunEvent
) -> bool:
    """Whether the run row and its head event describe one healthy head.

    Steady state is the current node's own last word. But a transition commits
    the run onto its heir in the same transaction that records the event, so
    until the heir writes its own first event the head still carries the
    transitioned node while `current_node_id` already names the heir. That
    window is healthy state, not corruption -- the restart sweep learned the
    same lesson for its driver families (#923) -- and it is recognized by
    recomputing the recorded transition's target. A verdict-steered loop exit
    cannot be recomputed without the verdict it was steered by and stays
    unrecognized, which only means it is judged as strictly as before.
    """
    if head_event.revision_hash != run.revision_hash:
        return False
    if head_event.node_execution_id != NodeExecutionId.for_node(
        run.run_id, run.revision_hash, head_event.node_id, head_event.round_ordinal
    ):
        return False
    if (
        head_event.node_id == run.current_node_id
        and head_event.round_ordinal == run.current_round_ordinal
    ):
        return True
    if run.state is not RunState.STARTED:
        return False
    try:
        completion = completion_after_node(
            graph, head_event.node_id, head_event.round_ordinal
        )
    except LoopVerdictNotRead:
        return False
    return completion == RunContinues(run.current_node_id, run.current_round_ordinal)


class DbosWaitAnswerer:
    def __init__(self, engine: Engine, application_version: str) -> None:
        self._engine = engine
        self._application_version = application_version

    def submit_result(self, request: SubmitWaitAnswerRequest) -> DurableAnswerResult:
        try:
            with self._engine.connect() as read_connection:
                document = read_connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == request.revision_hash.value
                    )
                )
            if document is None:
                prepared_document = None
                graph = None
            else:
                prepared_document = bytes(document)
                graph = graph_from_document(request.revision_hash, prepared_document)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

        client: DBOSClient | None = None
        try:
            client = DBOSClient(
                system_database_engine=self._engine, use_listen_notify=False
            )
            with self._engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    run_record = (
                        connection.execute(
                            sa.select(runs).where(runs.c.run_id == request.run_id.value)
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if run_record is None:
                        connection.rollback()
                        return DurableAnswerRunMissing()
                    run = run_from_record_with_bindings(connection, run_record)
                    if run.revision_hash != request.revision_hash:
                        connection.rollback()
                        return DurableAnswerRevisionConflict()
                    stored_document = connection.scalar(
                        sa.select(workflow_revisions.c.document).where(
                            workflow_revisions.c.revision_hash
                            == request.revision_hash.value
                        )
                    )
                    if (
                        prepared_document is None
                        or graph is None
                        or stored_document is None
                        or bytes(stored_document) != prepared_document
                    ):
                        connection.rollback()
                        return DurableStateCorrupt()
                    stored_revision = WorkflowRevision(bytes(stored_document))
                    if stored_revision.revision_hash != request.revision_hash:
                        connection.rollback()
                        return DurableStateCorrupt()
                    try:
                        current_node = graph.node(run.current_node_id)
                    except KeyError:
                        connection.rollback()
                        return DurableStateCorrupt()
                    current_execution_id = NodeExecutionId.for_node(
                        run.run_id,
                        run.revision_hash,
                        run.current_node_id,
                        run.current_round_ordinal,
                    )
                    head_records = tuple(
                        connection.execute(
                            sa.select(run_events).where(
                                run_events.c.run_id == run.run_id.value,
                                run_events.c.event_sequence == run.last_event_sequence,
                            )
                        ).mappings()
                    )
                    if len(head_records) != 1:
                        connection.rollback()
                        return DurableStateCorrupt()
                    head_event = event_from_record(head_records[0])
                    if not _run_stands_on_its_head_event(graph, run, head_event):
                        connection.rollback()
                        return DurableStateCorrupt()
                    current_record = _wait_answer_record(
                        connection, current_execution_id
                    )
                    current_snapshot = (
                        None
                        if current_record is None
                        else wait_answer_snapshot_from_record(current_record)
                    )
                    if (
                        current_snapshot is not None
                        and not _wait_answer_binds_execution(
                            current_snapshot,
                            run.run_id,
                            run.revision_hash,
                            run.current_node_id,
                            current_execution_id,
                            run.current_round_ordinal,
                        )
                    ):
                        connection.rollback()
                        return DurableStateCorrupt()
                    if run.state is RunState.WAITING_INPUT:
                        if (
                            not isinstance(current_node, WaitNodeV3)
                            or head_event.event_kind is not RunEventKind.WAITING_INPUT
                            or (
                                current_snapshot is not None
                                and current_snapshot.state is WaitAnswerState.APPLIED
                            )
                        ):
                            connection.rollback()
                            return DurableStateCorrupt()
                    elif current_snapshot is not None and (
                        current_snapshot.state is not WaitAnswerState.APPLIED
                        or head_event.event_kind is not RunEventKind.WAIT_ANSWERED
                    ):
                        connection.rollback()
                        return DurableStateCorrupt()
                    requested_record = _wait_answer_record(
                        connection, request.expected_node_execution_id
                    )
                    requested_snapshot = (
                        None
                        if requested_record is None
                        else wait_answer_snapshot_from_record(requested_record)
                    )
                    expected_events = _events_for_wait_execution(
                        connection, request.expected_node_execution_id
                    )
                    if expected_events and any(
                        event.run_id != request.run_id
                        or event.revision_hash != request.revision_hash
                        or event.node_id != request.node_id
                        or event.node_execution_id
                        != NodeExecutionId.for_node(
                            event.run_id,
                            event.revision_hash,
                            event.node_id,
                            event.round_ordinal,
                        )
                        for event in expected_events
                    ):
                        connection.rollback()
                        return DurableStateCorrupt()
                    waiting_events = tuple(
                        event
                        for event in expected_events
                        if event.event_kind is RunEventKind.WAITING_INPUT
                    )
                    if len(waiting_events) > 1:
                        connection.rollback()
                        return DurableStateCorrupt()
                    answered_events = tuple(
                        event
                        for event in expected_events
                        if event.event_kind is RunEventKind.WAIT_ANSWERED
                    )
                    if len(answered_events) > 1 or (
                        answered_events and requested_snapshot is None
                    ):
                        connection.rollback()
                        return DurableStateCorrupt()
                    if requested_snapshot is not None:
                        if (
                            not _wait_answer_binds_request(requested_snapshot, request)
                            or (
                                isinstance(
                                    requested_snapshot.answer.actor, WaitAnswerActor
                                )
                                and requested_snapshot.answer.actor != request.actor
                            )
                            or len(waiting_events) != 1
                        ):
                            connection.rollback()
                            return DurableStateCorrupt()
                        if (
                            requested_snapshot.answer.answer_bytes
                            != request.answer_bytes
                        ):
                            connection.rollback()
                            return DurableAnswerRoundAnswered()
                        if requested_snapshot.state is WaitAnswerState.APPLIED:
                            if not _applied_answer_matches_event(
                                requested_snapshot, expected_events, request.actor
                            ):
                                connection.rollback()
                                return DurableStateCorrupt()
                            connection.rollback()
                            return DurableAnswerExisting(requested_snapshot)
                        if answered_events:
                            connection.rollback()
                            return DurableStateCorrupt()
                        if (
                            request.expected_node_execution_id != current_execution_id
                            or run.state is not RunState.WAITING_INPUT
                            or head_event.event_kind is not RunEventKind.WAITING_INPUT
                            or head_event.wait_answer_actor != request.actor
                        ):
                            connection.rollback()
                            return DurableStateCorrupt()
                        connection.commit()
                        return DurableAnswerExisting(requested_snapshot)
                    if request.expected_node_execution_id != current_execution_id:
                        if not expected_events:
                            connection.rollback()
                            return DurableStateCorrupt()
                        connection.rollback()
                        return DurableAnswerStale()
                    if run.state is not RunState.WAITING_INPUT:
                        connection.rollback()
                        return DurableAnswerStateConflict()
                    if not isinstance(current_node, WaitNodeV3):
                        connection.rollback()
                        return DurableStateCorrupt()
                    if request.node_id != run.current_node_id:
                        connection.rollback()
                        return DurableStateCorrupt()
                    try:
                        node = graph.node(request.node_id)
                    except KeyError:
                        connection.rollback()
                        return DurableAnswerNodeMissing()
                    if node != current_node:
                        connection.rollback()
                        return DurableStateCorrupt()
                    round_ordinal = run.current_round_ordinal
                    execution_id = current_execution_id
                    answer = WaitAnswer(
                        request.run_id,
                        request.revision_hash,
                        request.node_id,
                        execution_id,
                        request.actor,
                        request.answer_bytes,
                        round_ordinal,
                    )
                    answer_workflow_id = answer_workflow_id_for(execution_id)
                    connection.execute(
                        wait_answers.insert().values(
                            run_id=answer.run_id.value,
                            revision_hash=answer.revision_hash.value,
                            node_id=answer.node_id,
                            node_execution_id=answer.node_execution_id.value,
                            round_ordinal=answer.round_ordinal,
                            actor=request.actor.value,
                            actor_attribution_kind=(
                                WaitAnswerAttributionKind.RECORDED.value
                            ),
                            answer_bytes=answer.answer_bytes,
                            answer_hash=answer.answer_hash.value,
                            answer_workflow_id=answer_workflow_id,
                            state=WaitAnswerState.PENDING.value,
                            state_version=0,
                        )
                    )
                    stored_record = _wait_answer_record(connection, execution_id)
                    if stored_record is None:
                        connection.rollback()
                        return DurableStateCorrupt()
                    snapshot = wait_answer_snapshot_from_record(stored_record)
                    unanswerable = why_a_wait_node_does_not_admit_an_answer(
                        connection, current_node, request.answer_bytes
                    )
                    if unanswerable is not None:
                        connection.rollback()
                        return DurableAnswerNotAdmitted(unanswerable)
                    options: EnqueueOptions = {
                        "workflow_name": ANSWER_WORKFLOW_NAME,
                        "queue_name": QUEUE_NAME,
                        "workflow_id": answer_workflow_id,
                        "app_version": self._application_version,
                    }
                    client.enqueue_in_transaction(
                        connection,
                        options,
                        answer.run_id.value,
                        answer.revision_hash.value,
                        answer.node_id,
                        answer.round_ordinal,
                    )
                    connection.commit()
                    return DurableAnswerCreated(snapshot)
                except (OperationalError, PoolTimeoutError):
                    connection.rollback()
                    return DurableWriteUnavailable()
                except (ValueError, RuntimeError, DatabaseError):
                    connection.rollback()
                    return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
        finally:
            if client is not None:
                client.destroy()
