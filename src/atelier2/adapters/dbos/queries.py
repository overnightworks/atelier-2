from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.agent_attempt_store import (
    attempt_from_record,
    compose_agent_node_job_for_attempt,
    load_output_schema_refusal_receipt,
    load_prior_output_schema_refusal_receipt,
)
from atelier2.adapters.dbos.artifact_store import (
    read_stored_artifact,
    read_stored_artifacts,
)
from atelier2.adapters.dbos.attention_events import load_attention_event_page
from atelier2.adapters.dbos.effect_store import (
    command_snapshot_from_record,
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.run_fork_store import (
    _stored_fork_for_command,
    validate_stored_fork,
)
from atelier2.adapters.dbos.run_store import (
    NodeOutputNotWritten,
    NodeOutputSchemaRefused,
    load_node_outputs,
    load_run_inputs,
    load_run_orders,
    wait_answer_snapshot_from_record,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    event_from_record,
    load_graph,
    runs_from_records_with_bindings,
    validate_run_graph_binding,
)
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    agent_receipts_v2,
    attempt_instants,
    catalog_source_intakes,
    effect_intents,
    effect_receipts,
    event_instants,
    node_receipts_v3,
    reconcile_commands,
    run_events,
    run_forks,
    run_instants,
    runs,
    wait_answers,
    workflow_revisions,
)
from atelier2.adapters.dbos.workflow import (
    _declared_output_schema_document,
    _pinned_maximum_assistant_turns,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bounded_process_cache import BoundedProcessCache
from atelier2.application.compose_node_job import node_job
from atelier2.application.project_node_rail import (
    never_launched_cleanup_on_failed_run,
    project_node_rail,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    OutputSchemaRefusalReceipt,
)
from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.catalog_v3 import CatalogActivatedAt
from atelier2.contracts.definition_sources import (
    DefinitionSourceId,
    RepositoryPath,
    RevisionProvenance,
    SourceCommit,
)
from atelier2.contracts.effects import (
    EffectIntentState,
    ReconcileCommandId,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    NodeExecutionId,
    RunEvent,
    RunEventKind,
    WaitAnswerAttribution,
    WaitAnswerAttributionKind,
    WaitAnswerState,
    logical_effect_key_for,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import PersistedReceiptDisposition
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS, MINIMUM_PAGE_ITEMS
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import AnyRun, RunV2, RunV3
from atelier2.contracts.run_events import (
    PersistedRunEvent,
    RunEventPage,
)
from atelier2.contracts.run_forks import (
    MAXIMUM_RUN_FORK_SUCCESSORS,
    RunForkCommandId,
)
from atelier2.contracts.run_projections import (
    AgentAttemptCancellationProjection,
    AgentAttemptProjection,
    DefectiveRunProjection,
    NodeAnswer,
    NodeDetail,
    NodeProvenance,
    NodeState,
    ReusedNodeProjection,
    RunForkOriginProjection,
    RunForkSuccessorProjection,
    RunPage,
    RunProjection,
    RunProjectionProblemCode,
    WaitingReconciliationProjection,
    bounded_run_row_defect_detail,
    execution_awaits_effect_reconciliation,
    public_agent_attempt_state,
)
from atelier2.contracts.runs import (
    RevisionHashCollision,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.secret_redaction import redact_credentials
from atelier2.contracts.stored_node_receipt_reasons import (
    read_stored_node_receipt_reason,
)
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    ListedWorkflowRevision,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from atelier2.contracts.workflows import round_of
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocument,
    SubworkflowNodeV3,
    WaitNodeV3,
    WorkflowGraphV3,
)
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    PrepareRunEventStreamResult,
    ReadAttentionEventPageResult,
    ReadRunEventPageResult,
    StreamReady,
)
from atelier2.ports.run_queries import (
    GetNodeDetailResult,
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunsResult,
    NodeDetailFound,
    NodeQueryMissing,
    ReconciliationRetryCommandConflict,
    ReconciliationRetryTargetFound,
    ReconciliationRetryTargetMissing,
    RunFound,
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    GetWorkflowRevisionResult,
    ListDescribedWorkflowRevisionsResult,
    ListWorkflowRevisionsResult,
    ProjectionLimitExceeded,
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
)


class WaitAnswerProjectionCorrupt(RuntimeError):
    """A WAIT_ANSWERED event contradicts its one durable answer record."""


_PARSED_WORKFLOW_REVISION_CACHE_CAPACITY = 4_096
"""Comfortably above the tens of workflow revisions one project holds today.

The cap bounds memory for a pathological project; it never bounds
correctness. A workflow revision is content-addressed (`revision_hash` is
derived from its bytes) and cannot change once published, so a parsed graph
stays correct for the process's whole lifetime -- there is no staleness to
invalidate.
"""


# #937 (round 3): profiling a described page after the lookup and settlement
# caches from rounds 1 and 2 found the dominant remaining cost was parsing
# every immutable revision again on every read. All `DbosQueries` instances
# share this module-level cache (`BoundedProcessCache`, round 4's extracted
# owner) so reads across pages and adapter instances pay that parse once per
# content hash.
#
# A failed parse is deliberately never cached: it proved no graph value, and
# remembering its exception would turn one transient parser or runtime failure
# into process-lifetime durable-state corruption instead of letting the next
# read retry.
_PARSED_WORKFLOW_REVISIONS: BoundedProcessCache[
    WorkflowRevisionHash, AnyWorkflowDocument
] = BoundedProcessCache(capacity=_PARSED_WORKFLOW_REVISION_CACHE_CAPACITY)


def _parsed_workflow_revision(
    revision: WorkflowRevision,
) -> AnyWorkflowDocument:
    cached = _PARSED_WORKFLOW_REVISIONS.found(revision.revision_hash)
    if cached is not None:
        return cached
    graph = parse_workflow_document(revision.document)
    _PARSED_WORKFLOW_REVISIONS.remember(revision.revision_hash, graph)
    return graph


_LENGTH_LABEL_PREFIX = "_atelier_length_"
_MAXIMUM_UTF8_BYTES_PER_CHARACTER = 4
_RUN_PROJECTION_COLUMNS: tuple[sa.Column[Any], ...] = (
    runs.c.run_id,
    runs.c.revision_hash,
    runs.c.workflow_format_version,
    runs.c.agent_binding_set_hash,
    runs.c.current_node_id,
    runs.c.current_round_ordinal,
    runs.c.state,
    runs.c.state_version,
    runs.c.last_event_sequence,
    runs.c.terminal_hash,
    # A V3 run reads back as `RunV3`, which is bound to the configuration
    # revision it was started under; without this column every projection of one
    # raises rather than answering. It stayed unnoticed while no V3 run could
    # reach a public route.
    runs.c.run_configuration_revision_hash,
)
_RUN_FIELD_COLUMNS = frozenset(("run_id", "current_node_id"))
_REVISION_DOCUMENT_COLUMNS = frozenset(("document",))
_INTENT_PAYLOAD_COLUMNS = frozenset(("canonical_request",))
_INTENT_FIELD_COLUMNS = frozenset(
    (
        "logical_key",
        "run_id",
        "adapter_revision",
        "destination_identity",
        "adapter_operational_identity",
        "reconciliation_owner_command_id",
    )
)
_COMMAND_PAYLOAD_COLUMNS = frozenset(("found_result",))
_COMMAND_FIELD_COLUMNS = frozenset(
    ("command_id", "logical_key", "actor", "evidence", "found_effect_id")
)
_EVENT_PAYLOAD_COLUMNS = frozenset(("payload",))
_EVENT_FIELD_COLUMNS = frozenset(("run_id", "node_id", "receipt_logical_key"))
_ATTEMPT_FIELD_COLUMNS = frozenset(
    ("executor_operational_identity", "run_id", "node_id")
)
_RECEIPT_PAYLOAD_COLUMNS = frozenset(("canonical_request", "result"))
_RECEIPT_FIELD_COLUMNS = frozenset(
    (
        "logical_key",
        "run_id",
        "adapter_revision",
        "destination_identity",
        "adapter_operational_identity",
        "effect_id",
        "reconcile_command_id",
    )
)
_RUN_FORK_FIELD_COLUMNS = frozenset(
    ("origin_run_id", "successor_run_id", "restart_from_node_id")
)

_FIRST_INTAKE_OF_ITS_REVISION = 1
_INTAKE_RANK = "intake_rank"


def _revision_provenance_rows() -> sa.Subquery:
    """Exactly one origin row per published revision, ready to be joined 1:1.

    A revision's origin is its *first* intake -- earliest instant, then source
    and path to settle a tie -- so a later delivery of bytes the catalog
    already holds never rewrites where they came from. Ranked rather than
    joined plainly: the intake table is keyed by source, path and intake
    number, so one revision may carry several rows, and a plain join would
    repeat that revision's whole document once per row, spend the page budget
    on duplicates and break the `limit + 1` a page counts with.

    Nothing but that row is read. Where the source stands today is
    configuration a later connect may change, and joining it here would answer
    an old delivery with a repository that never carried it.
    """

    ranked = sa.select(
        catalog_source_intakes.c.revision_kind,
        catalog_source_intakes.c.revision_hash,
        catalog_source_intakes.c.source_id,
        catalog_source_intakes.c.source_path,
        catalog_source_intakes.c.source_commit,
        catalog_source_intakes.c.intaken_at,
        sa.func.row_number()
        .over(
            partition_by=(
                catalog_source_intakes.c.revision_kind,
                catalog_source_intakes.c.revision_hash,
            ),
            order_by=(
                catalog_source_intakes.c.intaken_at,
                catalog_source_intakes.c.source_id,
                catalog_source_intakes.c.source_path,
            ),
        )
        .label(_INTAKE_RANK),
    ).subquery()
    return (
        sa.select(
            ranked.c.revision_kind,
            ranked.c.revision_hash,
            ranked.c.source_id,
            ranked.c.source_path,
            ranked.c.source_commit,
            ranked.c.intaken_at,
        )
        .where(ranked.c[_INTAKE_RANK] == _FIRST_INTAKE_OF_ITS_REVISION)
        .subquery()
    )


_REVISION_PROVENANCE = _revision_provenance_rows()
_REVISION_PROVENANCE_COLUMNS = (
    _REVISION_PROVENANCE.c.source_id,
    _REVISION_PROVENANCE.c.source_commit,
    _REVISION_PROVENANCE.c.source_path,
    _REVISION_PROVENANCE.c.intaken_at,
)


def _workflow_revisions_with_provenance() -> sa.Join:
    """Workflow revisions and, where a source delivered them, their origin.

    Outer, because a document published through the catalog's own door has no
    origin to name and is still a revision this catalog lists.
    """

    return workflow_revisions.outerjoin(
        _REVISION_PROVENANCE,
        sa.and_(
            _REVISION_PROVENANCE.c.revision_hash == workflow_revisions.c.revision_hash,
            _REVISION_PROVENANCE.c.revision_kind == RevisionKind.WORKFLOW.value,
        ),
    )


def _revision_provenance(record: Mapping[Any, Any]) -> RevisionProvenance | None:
    if record["source_id"] is None:
        return None
    return RevisionProvenance(
        DefinitionSourceId(str(record["source_id"])),
        SourceCommit(str(record["source_commit"])),
        RepositoryPath(str(record["source_path"])),
        CatalogActivatedAt(str(record["intaken_at"])),
    )


def _bounded_projection_select(
    table: sa.Table,
    projection_limit: DurableProjectionLimit,
    *,
    columns: Sequence[sa.Column[Any]] | None = None,
    document_columns: frozenset[str] = frozenset(),
    payload_columns: frozenset[str] = frozenset(),
    field_columns: frozenset[str] = frozenset(),
) -> sa.Select[Any]:
    selected_columns = tuple(table.c) if columns is None else columns
    projected: list[Any] = []
    for column in selected_columns:
        limit_exemption: sa.ColumnElement[bool] | None = None
        response_length = None
        if column.name in document_columns:
            maximum = projection_limit.maximum_document_bytes
            length = sa.func.length(column)
        elif column.name in payload_columns:
            maximum = projection_limit.maximum_payload_bytes
            length = sa.func.length(column)
            if table is run_events and column.name == run_events.c.payload.name:
                limit_exemption = (
                    run_events.c.event_kind == RunEventKind.WAITING_INPUT.value
                )
                # WAITING_INPUT's payload is private durable question identity:
                # all three public WAITING_INPUT event families omit it. Keep
                # selecting the exact bytes so event_from_record verifies
                # payload_hash and event_hash, while marking their
                # response-projected length as inapplicable.
                response_length = sa.case((limit_exemption, None), else_=length)
        elif column.name in field_columns:
            maximum = (
                _MAXIMUM_UTF8_BYTES_PER_CHARACTER
                * projection_limit.maximum_field_characters
            )
            length = sa.func.length(sa.cast(column, sa.LargeBinary()))
        else:
            projected.append(column)
            continue
        admitted = length <= maximum
        if limit_exemption is not None:
            admitted = sa.or_(limit_exemption, admitted)
        projected.append(sa.case((admitted, column), else_=None).label(column.name))
        projected.append(
            (length if response_length is None else response_length).label(
                _LENGTH_LABEL_PREFIX + column.name
            )
        )
    return sa.select(*projected)


def _validate_bounded_record(
    record: Mapping[Any, Any],
    projection_limit: DurableProjectionLimit,
    *,
    document_columns: frozenset[str] = frozenset(),
    payload_columns: frozenset[str] = frozenset(),
    field_columns: frozenset[str] = frozenset(),
) -> None:
    for column_name in document_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is not None:
            projection_limit.validate_document_length(int(length))
    for column_name in payload_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is not None:
            projection_limit.validate_payload_length(int(length))
    for column_name in field_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is None:
            continue
        value = record[column_name]
        if value is None:
            raise ProjectionLimitExceeded(
                "durable text exceeds its response allocation limit"
            )
        projection_limit.validate_field_length(len(str(value)))


_LOG = logging.getLogger("atelier2")

_AGENT_FAILURE_FORMATS = frozenset((WorkflowFormatVersion.V2, WorkflowFormatVersion.V3))
"""Which families reach the agent attempt path, and so can record its failure."""


def _run_ending_event_predicate(
    current_node_execution_id: NodeExecutionId,
) -> Callable[[Connection, Mapping[Any, Any]], bool]:
    """Whether one event is the event that ended this run.

    #194 H1b lifted the terminal condition off a dedicated terminal node onto
    the run, so no single event kind identifies an ending alone. The kind
    cannot, because every agent node completes or fails with the same pair, a
    linear Action completes with its own kind, and a Wait node's answer
    completes with a third. The execution cannot either, and that is the less
    obvious half: an attempt event can advance the run's head without moving
    it. What ends a run is the **completion or failure** of the exact
    execution it stands on, so both halves are asked. Exact identity also
    keeps an earlier round's completion at the same looped node from posing as
    the current round's ending.

    The four kinds below are exhaustive for every node the runtime can
    currently stand a run's sink on -- Agent (two kinds), Action and Wait
    (#510). A Deterministic or Subworkflow node has no execution path yet
    (`bind_node` refuses one before any event could be written for it), so
    neither belongs here until that gap is closed with its own runtime wiring.

    Two events of those kinds still end nothing, and both are asked only after
    the cheap halves have matched: an output-schema refusal that orders its own
    repair, and an agent success on a node whose own effect is still owed. Both
    read durable rows rather than the run's document, which keeps this cheap: it
    is a pre-flight before stream headers and a check beside a page read, never
    a projection.
    """

    ending = {
        RunEventKind.AGENT_COMPLETED.value,
        RunEventKind.AGENT_FAILED.value,
        RunEventKind.ACTION_COMPLETED.value,
        RunEventKind.WAIT_ANSWERED.value,
    }

    def ended_the_run(connection: Connection, record: Mapping[Any, Any]) -> bool:
        kind, execution_id = _event_endpoint(record)
        return (
            kind in ending
            and execution_id == current_node_execution_id
            and not _event_orders_output_schema_repair(connection, record)
            and not _agent_success_owes_its_node_effect(connection, record)
        )

    return ended_the_run


def _agent_success_owes_its_node_effect(
    connection: Connection, record: Mapping[Any, Any]
) -> bool:
    """Whether this agent success left its own node's platform effect to perform.

    A tool grant lets an agent node hold an effect (decision 0010): the run does
    not leave that node when the agent succeeds. It stands there while the effect
    is prepared, reconciled by the operator and completed, and the node's own
    `ACTION_COMPLETED` is what ends the run. The intent that effect is carried on
    is the row the reconciliation door reads, and it stays on the node's exact
    execution for good -- so every later read of this history, including the one
    after the effect was performed, still sees the success for what it was.
    """
    if str(record["event_kind"]) != RunEventKind.AGENT_COMPLETED.value:
        return False
    logical_key = logical_effect_key_for(
        NodeExecutionId(str(record["node_execution_id"]))
    )
    return (
        connection.scalar(
            sa.select(effect_intents.c.logical_key).where(
                effect_intents.c.logical_key == logical_key.value
            )
        )
        is not None
    )


def _node_execution_id(
    run: AnyRun, graph: AnyWorkflowDocument, node_id: str
) -> NodeExecutionId:
    return NodeExecutionId.for_node(
        run.run_id,
        run.revision_hash,
        node_id,
        round_of(graph, node_id, run.current_round_ordinal),
    )


def _event_endpoint(record: Mapping[Any, Any]) -> tuple[str, NodeExecutionId]:
    execution_id = NodeExecutionId(str(record["node_execution_id"]))
    expected = NodeExecutionId.for_node(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        str(record["node_id"]),
        int(record["round_ordinal"]),
    )
    if execution_id != expected:
        raise RunTransitionConflict("event node execution binding disagrees")
    return str(record["event_kind"]), execution_id


def _event_orders_output_schema_repair(
    connection: Connection, record: Mapping[Any, Any]
) -> bool:
    attempt_id = record.get("agent_attempt_id")
    return (
        str(record["event_kind"]) == RunEventKind.AGENT_FAILED.value
        and bytes(record["payload"])
        == AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED.value.encode("ascii")
        and int(record["attempt_ordinal"]) == 1
        and attempt_id is not None
        and _attempt_output_schema_refusal(
            connection,
            NodeExecutionId(str(record["node_execution_id"])),
            AgentAttemptId(str(attempt_id)),
        )
        is not None
    )


def _durable_attempt_state(persisted_value: Any) -> AgentAttemptState:
    try:
        return AgentAttemptState(str(persisted_value))
    except ValueError as outside_vocabulary:
        raise RunTransitionConflict(
            "persisted agent attempt state is outside the durable vocabulary"
        ) from outside_vocabulary


def _current_attempt_projection(
    record: Mapping[Any, Any],
    *,
    session: Connection,
    run: RunV2 | RunV3,
    graph: WorkflowGraphV3,
    effect_awaits_reconciliation: bool,
) -> AgentAttemptProjection:
    node = graph.node(run.current_node_id)
    if not isinstance(node, AgentNodeV3):
        raise RunTransitionConflict("current attempt does not belong to an agent")
    binding = next(
        (binding for binding in run.agent_bindings if binding.role.value == node.role),
        None,
    )
    if binding is None:
        raise RunTransitionConflict("current agent has no exact durable binding")
    operational_identity = AgentExecutorOperationalIdentity(
        str(record["executor_operational_identity"])
    )
    execution_id = _node_execution_id(run, graph, run.current_node_id)
    # Recomputed through the one composition owner, with everything that owner
    # is given: the orders the run was started with and the work earlier nodes
    # handed on. A recomputation that knew only part of it would answer a run
    # that really was a chain with a conflict about its own identity.
    request_hash = AgentExecutionRequestHash(str(record["request_hash"]))
    ordinal = int(record["attempt_ordinal"])
    output_schema = _declared_output_schema_document(session, node)

    def request_for(authored_job: bytes) -> AgentExecutionRequestV2:
        return AgentExecutionRequestV2(
            execution_id,
            run.run_id,
            run.revision_hash,
            run.current_node_id,
            binding,
            operational_identity,
            authored_job,
            None if output_schema is None else output_schema.encode("utf-8"),
            run.current_round_ordinal,
            _pinned_maximum_assistant_turns(session, node),
        )

    orders = load_run_inputs(session, run.run_id, node)
    results = load_node_outputs(
        session,
        run.run_id,
        run.revision_hash,
        graph,
        node,
        run.current_round_ordinal,
    )
    attempt_id = AgentAttemptId(str(record["attempt_id"]))
    repair_receipt = load_prior_output_schema_refusal_receipt(
        session,
        target_attempt_id=attempt_id,
        target_node_execution_id=execution_id,
        target_attempt_ordinal=ordinal,
        expected_schema_revision=PublishedRevisionHash(
            node.outputs[0].schema_reference.revision
        ),
    )
    exact_request = request_for(
        compose_agent_node_job_for_attempt(
            node,
            orders,
            results,
            target_node_execution_id=execution_id,
            target_attempt_ordinal=ordinal,
            prior_refusal_receipt=repair_receipt,
        )
    )
    expected_attempt_id = AgentAttemptId.for_execution(
        execution_id, exact_request.request_hash, ordinal
    )
    if (
        ordinal not in (1, 2)
        or NodeExecutionId(str(record["node_execution_id"])) != execution_id
        or RunId(str(record["run_id"])) != run.run_id
        or WorkflowRevisionHash(str(record["workflow_revision_hash"]))
        != run.revision_hash
        or str(record["node_id"]) != run.current_node_id
        or request_hash != exact_request.request_hash
        or attempt_id != expected_attempt_id
    ):
        raise RunTransitionConflict(
            "current agent attempt binding disagrees "
            f"run_id durable={str(record['run_id'])!r} expected={run.run_id.value!r} "
            f"node_id durable={str(record['node_id'])!r} expected={run.current_node_id!r} "
            f"ordinal={ordinal!r} "
            f"request_hash durable={request_hash.value!r} "
            f"expected={exact_request.request_hash.value!r} "
            f"attempt_id durable={attempt_id.value!r} expected={expected_attempt_id.value!r} "
            f"node_execution_id durable={str(record['node_execution_id'])!r} "
            f"expected={execution_id.value!r} "
            f"workflow_revision_hash durable={str(record['workflow_revision_hash'])!r} "
            f"expected={run.revision_hash.value!r}"
        )
    durable_state = _durable_attempt_state(record["state"])
    public_state = public_agent_attempt_state(
        durable_state, effect_awaits_reconciliation=effect_awaits_reconciliation
    )
    if public_state is None:
        raise RunTransitionConflict(
            "successful current attempt has neither an atomic successor transition "
            "nor an effect awaiting reconciliation"
        )
    failure_value = record["failure_code"]
    receipt_value = record["receipt_hash"]
    state_version = int(record["state_version"])
    failure: AgentAttemptFailureCode | None = None
    if durable_state is AgentAttemptState.PREPARED:
        if state_version not in (0, 1) or receipt_value is not None:
            raise RunTransitionConflict("prepared agent attempt shape disagrees")
    elif durable_state is AgentAttemptState.LAUNCH_ARMED:
        if state_version < 1 or receipt_value is not None:
            raise RunTransitionConflict("armed agent attempt shape disagrees")
    elif durable_state is AgentAttemptState.FAILED:
        if state_version < 2 or receipt_value is not None:
            raise RunTransitionConflict("failed agent attempt shape disagrees")
        failure = AgentAttemptFailureCode(str(failure_value))
    elif durable_state is AgentAttemptState.SUCCEEDED:
        if state_version < 2 or receipt_value is None:
            raise RunTransitionConflict("succeeded agent attempt shape disagrees")
    elif durable_state in {
        AgentAttemptState.CANCEL_REQUESTED,
        AgentAttemptState.CANCELLED,
        AgentAttemptState.INTERRUPTED,
    }:
        if receipt_value is not None or record["cancellation_command_id"] is None:
            raise RunTransitionConflict("cancelled agent attempt shape disagrees")
    else:
        raise RunTransitionConflict("agent attempt state has no projected shape")
    if (failure_value is None) != (failure is None):
        raise RunTransitionConflict("current agent attempt failure shape disagrees")
    command_id = record["cancellation_command_id"]
    disposition = record["cancellation_disposition"]
    cancellation = (
        None
        if command_id is None
        else AgentAttemptCancellationProjection(
            str(command_id),
            AgentAttemptReplacement(str(record["replacement"])),
            AgentAttemptRedriveState(str(record["redrive_state"])),
            (
                None
                if disposition is None
                else AgentAttemptCancellationDisposition(str(disposition))
            ),
        )
    )
    return AgentAttemptProjection(
        attempt_id,
        execution_id,
        request_hash,
        ordinal,
        public_state,
        failure,
        cancellation,
    )


def _attempt_output_schema_refusal(
    connection: Connection,
    execution_id: NodeExecutionId,
    attempt_id: AgentAttemptId | None = None,
) -> OutputSchemaRefusalReceipt | None:
    attempt_record = (
        connection.execute(
            sa.select(agent_attempts).where(
                agent_attempts.c.node_execution_id == execution_id.value,
                (
                    agent_attempts.c.attempt_id == attempt_id.value
                    if attempt_id is not None
                    else agent_attempts.c.attempt_ordinal == 1
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if attempt_record is None:
        return None
    attempt = attempt_from_record(attempt_record)
    graph = load_graph(connection, attempt.workflow_revision_hash)
    node = graph.node(attempt.node_id)
    if not isinstance(node, AgentNodeV3):
        return None
    return load_output_schema_refusal_receipt(
        connection,
        attempt.attempt_id,
        expected_node_execution_id=execution_id,
        expected_attempt_ordinal=attempt.attempt_ordinal,
        expected_schema_revision=PublishedRevisionHash(
            node.outputs[0].schema_reference.revision
        ),
    )


def _node_receipt_refusal(
    connection: Connection,
    execution_id: NodeExecutionId,
    attempt_id: AgentAttemptId | None = None,
) -> str | None:
    """The durably named reason this node's execution ended without a success.

    An event supplies its exact Attempt identity, so that Attempt's immutable
    refusal receipt is its reason even after a later repair ends differently.
    An ordinary node read has no Attempt identity and prefers the terminal
    `node-receipt/v3`; only between rounds, while that terminal row is absent,
    does ordinal one's Attempt receipt name the active refusal. A succeeded
    node receipt refuses nothing, and a run from before either family's writer
    stays honestly absent.
    """
    if attempt_id is not None:
        exact_refusal = _attempt_output_schema_refusal(
            connection, execution_id, attempt_id
        )
        if exact_refusal is not None:
            return exact_refusal.reason
    record = connection.execute(
        sa.select(node_receipts_v3.c.disposition, node_receipts_v3.c.reason).where(
            node_receipts_v3.c.node_execution_id == execution_id.value
        )
    ).one_or_none()
    if record is None:
        refusal = _attempt_output_schema_refusal(connection, execution_id)
        return None if refusal is None else refusal.reason
    disposition = PersistedReceiptDisposition(str(record.disposition))
    if disposition is PersistedReceiptDisposition.SUCCEEDED:
        return None
    reason, _schema_revision, _value_hash = read_stored_node_receipt_reason(
        str(record.reason)
    )
    return reason


def _refusal_output_without_terminal_receipt(
    connection: Connection,
    execution_id: NodeExecutionId,
) -> NodeAnswer | None:
    """Ordinal one's own immutable Attempt receipt, before any `node-receipt/v3` exists.

    Shared tail of `_node_receipt_refusal_output` (single execution) and the
    page-batched terminal-result assembly (#1045): both fall back here only
    once the batched or single `node_receipts_v3` read named no row.
    """
    refusal = _attempt_output_schema_refusal(connection, execution_id)
    if refusal is None:
        return None
    value_hash = refusal.value_hash
    if refusal.artifact_hash is None:
        return NodeAnswer(b"", value_hash)
    artifact = read_stored_artifact(connection, refusal.artifact_hash)
    if artifact is None:
        raise RuntimeError("output-schema refusal artifact is missing")
    text = artifact.content.decode("utf-8")
    return NodeAnswer(redact_credentials(text).text.encode("utf-8"), value_hash)


def _refusal_output_from_receipt_reason(
    connection: Connection, reason: str
) -> NodeAnswer | None:
    """A redacted presentation of a terminal `node-receipt/v3` row's own reason.

    Shared tail of `_node_receipt_refusal_output` (single execution) and the
    page-batched terminal-result assembly (#1045): both already know the
    receipt's own `reason` column -- one from its own query, the other from a
    single batched read over the whole page -- so this is the one place that
    turns it into artifact bytes and redacts them (#664). A nonempty hash
    whose artifact is absent or disagrees is corrupt durable state, never an
    absent answer.
    """
    _reason, _schema_revision, value_hash = read_stored_node_receipt_reason(reason)
    if value_hash is None:
        return None
    artifact = read_stored_artifact(connection, ArtifactHash(value_hash.value))
    if artifact is None:
        if value_hash == Sha256Hash.of(b""):
            return NodeAnswer(b"", value_hash)
        raise RuntimeError("refused node output artifact is missing")
    text = artifact.content.decode("utf-8")
    return NodeAnswer(redact_credentials(text).text.encode("utf-8"), value_hash)


def _node_receipt_refusal_output(
    connection: Connection,
    execution_id: NodeExecutionId,
) -> NodeAnswer | None:
    """A redacted presentation of what a schema owner judged and refused.

    A terminal ordinal-two refusal names its value hash in `node-receipt/v3`;
    before that terminal row exists, ordinal one's immutable Attempt receipt
    names the same evidence for its nonterminal repair event
    (`_refusal_output_without_terminal_receipt`). A plain reason, an unjudged
    failure, or absence from both receipt families has nothing to resolve and
    reads honestly absent. Where either receipt names a hash, its failure
    transaction also published these exact bytes as an artifact under that
    same address (#664) -- so a reader who wants to see what was refused, not
    just that it was, reads them back through the one content-addressed store
    every other artifact uses (`_refusal_output_from_receipt_reason`).

    A provider's refused output is untrusted text on its way to a browser, and
    a schema refusal is exactly the shape of episode where a provider might
    have echoed a credential it was handed -- so `redact_credentials` runs over
    it here, at the read boundary, before this projection's caller ever builds
    a wire resource from it (#664). Bytes that do not decode as UTF-8 cannot be
    scanned for a credential shape at all, so the durable read fails loud
    instead of hiding or exposing them.
    """
    record = connection.execute(
        sa.select(node_receipts_v3.c.disposition, node_receipts_v3.c.reason).where(
            node_receipts_v3.c.node_execution_id == execution_id.value
        )
    ).one_or_none()
    if record is None:
        return _refusal_output_without_terminal_receipt(connection, execution_id)
    disposition = PersistedReceiptDisposition(str(record.disposition))
    if disposition is PersistedReceiptDisposition.SUCCEEDED:
        return None
    return _refusal_output_from_receipt_reason(connection, str(record.reason))


def _node_transcript(
    connection: Connection,
    execution_id: NodeExecutionId,
) -> AttemptTranscript | None:
    """The current execution's highest attempt that named a transcript.

    A null pointer is honest absence: this attempt decoded nothing, or none of
    its attempts have ended with a transcript yet. A named address whose
    artifact is missing, whose stored bytes do not hash to that address, or
    whose document `from_document` refuses is a store disagreeing with itself
    -- not an omitted transcript. The surrounding query maps that loud failure
    to durable corruption.
    """

    named = connection.scalar(
        sa.select(agent_attempts.c.transcript_artifact_hash)
        .where(
            agent_attempts.c.node_execution_id == execution_id.value,
            agent_attempts.c.transcript_artifact_hash.is_not(None),
        )
        .order_by(agent_attempts.c.attempt_ordinal.desc())
        .limit(1)
    )
    if named is None:
        return None
    artifact = read_stored_artifact(connection, ArtifactHash(str(named)))
    if artifact is None:
        raise ValueError(f"named transcript artifact {named} is missing from the store")
    return AttemptTranscript.from_document(artifact.content)


def _abandoned_intent_refusal(projection: RunProjection, node_id: str) -> str | None:
    """ABANDONED, when this node is the prepared effect the ended run never resolved."""

    reconciliation = projection.reconciliation
    if (
        reconciliation is None
        or reconciliation.intent.state is not EffectIntentState.ABANDONED
        or projection.run.current_node_id != node_id
    ):
        return None
    return EffectIntentState.ABANDONED.value


def _unavailable_executor_refusal(
    connection: Connection,
    execution_id: NodeExecutionId,
) -> str | None:
    """Read the terminal pre-attempt refusal written for a declared binding.

    This durable terminal event deliberately has no node receipt or attempt:
    the executor was known to be unavailable before a provider invocation could
    begin. Its product reason is nevertheless part of the run's own record,
    rather than a fresh current-host recomputation.
    """
    event = connection.execute(
        sa.select(run_events.c.payload).where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
            run_events.c.agent_attempt_id.is_(None),
            run_events.c.attempt_ordinal.is_(None),
        )
    ).one_or_none()
    if event is None:
        return None
    if event.payload != AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode(
        "ascii"
    ):
        return None
    return AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value


def _node_job_and_refusal(
    connection: Connection,
    projection: RunProjection,
    node: object,
    round_ordinal: int,
    node_state: NodeState,
) -> tuple[bytes | None, str | None, str | None]:
    """What this node was handed, and what stops it if something does.

    A no-input Wait's job is its authored prompt, and there is nothing to
    refuse. An input-bearing Wait that has paused reads the exact question
    from that execution's durable event; composition is only a preview before
    the pause or a diagnostic when a corrupt nonlive execution lacks its pause.
    An Agent job is composed from its authored opening and declared inputs.

    Composition is exactly where a refusal surfaces, because reading an earlier
    node's value against the schema its author pinned happens there. The
    composer's refusal is caught rather than raised: an operator asking about a
    stuck node wants to be told the reason, not to be refused the question.
    """

    if isinstance(node, WaitNodeV3):
        if not node.inputs:
            question_bytes = node.prompt.encode("utf-8")
            return question_bytes, Sha256Hash.of(question_bytes).value, None
        run = projection.run
        waiting_event = _waiting_input_event(
            connection,
            NodeExecutionId.for_node(
                run.run_id, run.revision_hash, node.id, round_ordinal
            ),
        )
        if waiting_event is not None:
            return (
                waiting_event.payload,
                waiting_event.payload_hash.value,
                None,
            )
        try:
            question = node_job(
                node.prompt,
                load_run_inputs(connection, run.run_id, node),
                load_node_outputs(
                    connection,
                    run.run_id,
                    run.revision_hash,
                    projection.graph,
                    node,
                    round_ordinal,
                ),
            )
            question_bytes = question.encode("utf-8")
        except NodeOutputNotWritten as not_written:
            if node_state in (NodeState.QUEUED, NodeState.WORKING):
                return None, None, None
            return None, None, str(not_written)
        except NodeOutputSchemaRefused as refused:
            return None, None, str(refused)
        if node_state not in (NodeState.QUEUED, NodeState.WORKING):
            raise RunTransitionConflict(
                "an input-bearing Wait that already paused carries no "
                "WAITING_INPUT event"
            )
        return question_bytes, Sha256Hash.of(question_bytes).value, None
    if not isinstance(node, AgentNodeV3):
        return None, None, None
    run = projection.run
    try:
        composed = node_job(
            node.instruction,
            load_run_inputs(connection, run.run_id, node),
            load_node_outputs(
                connection,
                run.run_id,
                run.revision_hash,
                projection.graph,
                node,
                round_ordinal,
            ),
        ).encode("utf-8")
    except NodeOutputNotWritten:
        # Absence, not refusal. The node this one reads has not written yet, so
        # there is no job to prove and nothing has judged anything. Saying so as
        # a refusal would report a waiting run as a stopped one.
        return None, None, None
    except NodeOutputSchemaRefused as refused:
        return None, None, str(refused)
    return composed, Sha256Hash.of(composed).value, None


def _waiting_input_event(
    connection: Connection, execution_id: NodeExecutionId
) -> RunEvent | None:
    """The exact durable pause for this Wait execution, integrity-checked."""
    record = (
        connection.execute(
            sa.select(run_events).where(
                run_events.c.node_execution_id == execution_id.value,
                run_events.c.event_kind == RunEventKind.WAITING_INPUT.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if record is None else event_from_record(record)


def _node_detail_execution(
    connection: Connection,
    projection: RunProjection,
    node_id: str,
    node: object,
    node_state: NodeState,
) -> tuple[int, NodeExecutionId]:
    """The execution the node rail is displaying.

    A queued or working node belongs to the round the run is turning, even when
    no execution event exists yet. An input-bearing Wait displayed as paused,
    answered or cancelled instead takes its execution from its own latest pause
    event. This distinction matters after another loop advances the run's round:
    the run head then no longer names the round in which an earlier bound Wait
    actually ran. A no-input Wait keeps its earlier current-round read path.
    """

    run = projection.run
    current_round = round_of(
        projection.graph,
        node_id,
        run.current_round_ordinal,
    )
    current_execution = NodeExecutionId.for_node(
        run.run_id,
        run.revision_hash,
        node_id,
        current_round,
    )
    if (
        not isinstance(node, WaitNodeV3)
        or not node.inputs
        or node_state
        not in (
            NodeState.NEEDS_YOU,
            NodeState.SUCCEEDED,
            NodeState.CANCELLED,
        )
    ):
        return current_round, current_execution

    record = (
        connection.execute(
            sa.select(run_events)
            .where(
                run_events.c.run_id == run.run_id.value,
                run_events.c.revision_hash == run.revision_hash.value,
                run_events.c.node_id == node_id,
                run_events.c.event_kind == RunEventKind.WAITING_INPUT.value,
            )
            .order_by(run_events.c.event_sequence.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return current_round, current_execution
    event = event_from_record(record)
    if node_state is NodeState.NEEDS_YOU and event.round_ordinal != current_round:
        return current_round, current_execution
    return event.round_ordinal, event.node_execution_id


def _node_instants(
    connection: Connection, execution_id: NodeExecutionId
) -> tuple[RecordedAt | None, RecordedAt | None]:
    """The first start and last end recorded for this node's attempts.

    An Agent node's attempts carry that window in their own instants table. A
    Wait node has no attempt row at all -- nothing runs between the run
    reaching it and a person answering it -- so an empty attempt result falls
    through to the single instant its answer was recorded at.
    """

    rows = tuple(
        connection.execute(
            sa.select(attempt_instants.c.started_at, attempt_instants.c.ended_at)
            .select_from(
                attempt_instants.join(
                    agent_attempts,
                    attempt_instants.c.attempt_id == agent_attempts.c.attempt_id,
                )
            )
            .where(agent_attempts.c.node_execution_id == execution_id.value)
            .order_by(agent_attempts.c.attempt_ordinal)
        ).mappings()
    )
    if not rows:
        return _node_wait_answered_instant(connection, execution_id)
    started = RecordedAt(str(rows[0]["started_at"]))
    ended_values = [record["ended_at"] for record in rows]
    if any(value is None for value in ended_values):
        return started, None
    return started, RecordedAt(str(max(str(value) for value in ended_values)))


def _node_wait_answered_instant(
    connection: Connection, execution_id: NodeExecutionId
) -> tuple[RecordedAt | None, RecordedAt | None]:
    """A Wait node's window: the one instant its answer was recorded at.

    Unlike an Agent attempt, a Wait node has no separate started/ended pair to
    read -- the person's answer is the only thing that happened, so it stands
    for both ends of the window. `event_instants` exists from V22 on; a run
    answered before that build wrote no such row, and the honest read for it is
    nothing, not a guess.
    """

    recorded_at = connection.execute(
        sa.select(event_instants.c.recorded_at)
        .select_from(
            run_events.join(
                event_instants,
                sa.and_(
                    event_instants.c.run_id == run_events.c.run_id,
                    event_instants.c.event_sequence == run_events.c.event_sequence,
                ),
            )
        )
        .where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value,
        )
    ).scalar()
    if recorded_at is None:
        return None, None
    instant = RecordedAt(str(recorded_at))
    return instant, instant


ANSWER_BEARING_EVENT_KINDS: frozenset[str] = frozenset(
    kind.value
    for kind in (
        RunEventKind.AGENT_COMPLETED,
        RunEventKind.WAIT_ANSWERED,
        RunEventKind.ACTION_COMPLETED,
        RunEventKind.SUBWORKFLOW_COMPLETED,
    )
)
"""Every event kind whose payload is a node's produced value.

This is its own set rather than a reuse of `_run_ending_event_predicate`'s
ending kinds: that one names what closes a run's current execution, scoped to
the node kinds that can stand a run's sink today. This one names what a
value-bearing write looks like at all, read by the batched ended-run scan
below across every node kind at once. `_node_answer` asks for a single node's
own kind instead of this whole set -- an `AgentNodeV3` that redeems a granted
platform effect (a push, an open-pr) also writes that effect's own
`ACTION_COMPLETED` confirmation under the same node-execution id as its
`AGENT_COMPLETED` completion (`commit_confirmed_effect` in `run_store.py`), so
an execution can carry two members of this set without carrying two answers.
"""


def _own_answer_event_kind(node: object) -> RunEventKind:
    """The one event kind that carries this node's own declared output."""

    match node:
        case AgentNodeV3():
            return RunEventKind.AGENT_COMPLETED
        case WaitNodeV3():
            return RunEventKind.WAIT_ANSWERED
        case ActionNodeV3():
            return RunEventKind.ACTION_COMPLETED
        case SubworkflowNodeV3():
            return RunEventKind.SUBWORKFLOW_COMPLETED
        case _:
            raise TypeError(f"node kind {type(node).__name__} has no declared answer")


def _embedded_platform_effect_kind(node: object) -> RunEventKind | None:
    """The one other answer-bearing kind this node's own execution may carry.

    `commit_confirmed_effect` (`run_store.py`) lets an `AgentNodeV3` redeem a
    granted platform effect (a push, an open-pr) in the same execution as its
    own completion, writing that effect's `ACTION_COMPLETED` confirmation
    under the node's own execution id alongside its `AGENT_COMPLETED` answer.
    Every other node kind's execution carries its own answer and nothing
    else, so any second answer-bearing event on it is durable state
    disagreeing with itself.
    """

    return RunEventKind.ACTION_COMPLETED if isinstance(node, AgentNodeV3) else None


def _node_answer(
    connection: Connection,
    node: object,
    execution_id: NodeExecutionId,
) -> NodeAnswer | None:
    """The value this node wrote, or nothing when it has written none yet.

    Matched against this node's own declared completion kind rather than any
    answer-bearing kind: a node execution can carry a second, embedded
    platform-effect confirmation that is not this node's answer (see
    `_embedded_platform_effect_kind`) and is skipped here; any other, wider
    disagreement -- a kind neither the node's own nor that one recognized
    companion -- still refuses loudly rather than being read past.
    """

    own_kind = _own_answer_event_kind(node)
    embedded_kind = _embedded_platform_effect_kind(node)
    record = None
    for candidate in connection.execute(
        sa.select(
            run_events.c.event_kind, run_events.c.payload, run_events.c.payload_hash
        ).where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind.in_(ANSWER_BEARING_EVENT_KINDS),
        )
    ):
        if str(candidate.event_kind) == own_kind.value:
            if record is not None:
                raise RunTransitionConflict(
                    "a node execution has more than one answer-bearing event"
                )
            record = candidate
        elif embedded_kind is None or str(candidate.event_kind) != embedded_kind.value:
            raise RunTransitionConflict(
                "a node execution has more than one answer-bearing event"
            )
    if record is None:
        return None
    return NodeAnswer(bytes(record.payload), Sha256Hash(str(record.payload_hash)))


def _run_terminal_results(
    connection: Connection,
    ended_runs: Sequence[RunV3],
    graphs: Mapping[WorkflowRevisionHash, AnyWorkflowDocument],
) -> dict[str, tuple[NodeAnswer | None, NodeAnswer | None]]:
    """Every ended run's own terminal answer and refusal, batched once per page.

    A page of History rows used to cost at least two statements per ended run,
    and every receiptless or refused row added one more on top of that (#1045
    REVISE C1, twice). Every ended run's terminal execution id is the same
    deterministic identity `current_node_execution_id` already names on the
    wire -- `current_node_id` at `current_round_ordinal` -- so every source
    below is read once for the whole page, keyed by that identity, and
    assembled per run afterward with no further query:

    - the answer-bearing event (`run_events`);
    - the terminal `node-receipt/v3` disposition and reason;
    - for an execution no terminal receipt names yet, its ordinal-one
      `agent_attempts` row -- the node kind is read from `graphs`, already
      parsed for this same page, never a second workflow-revision read;
    - that attempt's own `agent_attempt_receipts_v3` row, when its node is an
      agent node (only those ever write one);
    - every artifact either refusal path names, in one final read keyed by
      hash (`read_stored_artifacts`).

    A node execution that wrote more than one answer-bearing event of a kind
    its own declared type -- or its one recognized embedded platform-effect
    companion, `_embedded_platform_effect_kind` -- does not own is durable
    state disagreeing with itself: the single-execution `_node_answer` already
    refuses that loudly, and this batched read keeps the same refusal rather
    than a dict silently keeping the last one seen or a companion's own row
    silently masking the disagreement.

    This omits `load_output_schema_refusal_receipt`'s own re-verification of
    an attempt's schema revision and receipt hash against its expectations
    (`agent_attempt_store.py`): those defend the *live* repair path a fresh
    attempt is armed from. This projection only shows a reader what a
    finished run already wrote, and the one property that read depends on --
    the artifact's own bytes hashing to its address -- is still checked by
    `read_stored_artifacts`.
    """
    if not ended_runs:
        return {}
    execution_by_run_id: dict[str, NodeExecutionId] = {}
    own_answer_kind_by_execution: dict[str, RunEventKind] = {}
    embedded_effect_kind_by_execution: dict[str, RunEventKind | None] = {}
    for run in ended_runs:
        execution = NodeExecutionId.for_node(
            run.run_id,
            run.revision_hash,
            run.current_node_id,
            run.current_round_ordinal,
        )
        node = graphs[run.revision_hash].node(run.current_node_id)
        execution_by_run_id[run.run_id.value] = execution
        own_answer_kind_by_execution[execution.value] = _own_answer_event_kind(node)
        embedded_effect_kind_by_execution[execution.value] = (
            _embedded_platform_effect_kind(node)
        )
    execution_values = tuple(
        execution.value for execution in execution_by_run_id.values()
    )

    answers_by_execution: dict[str, NodeAnswer] = {}
    for record in connection.execute(
        sa.select(
            run_events.c.node_execution_id,
            run_events.c.event_kind,
            run_events.c.payload,
            run_events.c.payload_hash,
        ).where(
            run_events.c.node_execution_id.in_(execution_values),
            run_events.c.event_kind.in_(ANSWER_BEARING_EVENT_KINDS),
        )
    ):
        execution_value = str(record.node_execution_id)
        event_kind = str(record.event_kind)
        if event_kind == own_answer_kind_by_execution[execution_value].value:
            if execution_value in answers_by_execution:
                raise RunTransitionConflict(
                    "a node execution has more than one answer-bearing event"
                )
            answers_by_execution[execution_value] = NodeAnswer(
                bytes(record.payload), Sha256Hash(str(record.payload_hash))
            )
            continue
        embedded_kind = embedded_effect_kind_by_execution[execution_value]
        if embedded_kind is None or event_kind != embedded_kind.value:
            raise RunTransitionConflict(
                "a node execution has more than one answer-bearing event"
            )

    receipts_by_execution = {
        str(record.node_execution_id): record
        for record in connection.execute(
            sa.select(
                node_receipts_v3.c.node_execution_id,
                node_receipts_v3.c.disposition,
                node_receipts_v3.c.reason,
            ).where(node_receipts_v3.c.node_execution_id.in_(execution_values))
        )
    }

    run_by_id = {run.run_id.value: run for run in ended_runs}
    receiptless_execution_values = tuple(
        execution.value
        for execution in execution_by_run_id.values()
        if execution.value not in receipts_by_execution
    )

    attempts_by_execution: dict[str, Mapping[Any, Any]] = {}
    if receiptless_execution_values:
        for record in connection.execute(
            sa.select(
                agent_attempts.c.node_execution_id,
                agent_attempts.c.attempt_id,
                agent_attempts.c.node_id,
                agent_attempts.c.run_id,
            ).where(
                agent_attempts.c.node_execution_id.in_(receiptless_execution_values),
                agent_attempts.c.attempt_ordinal == 1,
            )
        ).mappings():
            attempts_by_execution[str(record["node_execution_id"])] = record

    agent_attempt_id_by_execution: dict[str, str] = {}
    for execution_value, attempt_record in attempts_by_execution.items():
        run = run_by_id[str(attempt_record["run_id"])]
        node = graphs[run.revision_hash].node(str(attempt_record["node_id"]))
        if isinstance(node, AgentNodeV3):
            agent_attempt_id_by_execution[execution_value] = str(
                attempt_record["attempt_id"]
            )

    attempt_receipts_by_execution: dict[str, Mapping[Any, Any]] = {}
    if agent_attempt_id_by_execution:
        execution_by_attempt_id = {
            attempt_id: execution_value
            for execution_value, attempt_id in agent_attempt_id_by_execution.items()
        }
        for record in connection.execute(
            sa.select(
                agent_attempt_receipts_v3.c.attempt_id,
                agent_attempt_receipts_v3.c.value_hash,
                agent_attempt_receipts_v3.c.artifact_hash,
            ).where(
                agent_attempt_receipts_v3.c.attempt_id.in_(
                    tuple(agent_attempt_id_by_execution.values())
                )
            )
        ).mappings():
            execution_value = execution_by_attempt_id[str(record["attempt_id"])]
            attempt_receipts_by_execution[execution_value] = record

    artifact_hashes: set[str] = set()
    for receipt_record in receipts_by_execution.values():
        disposition = PersistedReceiptDisposition(str(receipt_record.disposition))
        if disposition is PersistedReceiptDisposition.SUCCEEDED:
            continue
        _reason, _schema_revision, value_hash = read_stored_node_receipt_reason(
            str(receipt_record.reason)
        )
        if value_hash is not None and value_hash != Sha256Hash.of(b""):
            artifact_hashes.add(value_hash.value)
    for attempt_receipt in attempt_receipts_by_execution.values():
        artifact_hash = attempt_receipt["artifact_hash"]
        attempt_value_hash = str(attempt_receipt["value_hash"])
        if artifact_hash is None:
            if attempt_value_hash != Sha256Hash.of(b"").value:
                raise RunTransitionConflict(
                    "nonempty output-schema refusal has no artifact"
                )
            continue
        # The mirror of `load_output_schema_refusal_receipt`'s own check
        # (agent_attempt_store.py): the artifact an attempt names is always
        # addressed by the same hash it judged, so the two disagreeing is the
        # store contradicting itself, not a value this projection may show.
        if str(artifact_hash) != attempt_value_hash:
            raise RunTransitionConflict(
                "output-schema refusal artifact differs from its value hash"
            )
        artifact_hashes.add(str(artifact_hash))

    artifacts_by_hash = read_stored_artifacts(
        connection, tuple(ArtifactHash(value) for value in artifact_hashes)
    )

    def refusal_output_for(execution_value: str) -> NodeAnswer | None:
        receipt_record = receipts_by_execution.get(execution_value)
        if receipt_record is not None:
            disposition = PersistedReceiptDisposition(str(receipt_record.disposition))
            if disposition is PersistedReceiptDisposition.SUCCEEDED:
                return None
            _reason, _schema_revision, value_hash = read_stored_node_receipt_reason(
                str(receipt_record.reason)
            )
            if value_hash is None:
                return None
            if value_hash == Sha256Hash.of(b""):
                return NodeAnswer(b"", value_hash)
            artifact = artifacts_by_hash.get(value_hash.value)
            if artifact is None:
                raise RuntimeError("refused node output artifact is missing")
            text = artifact.content.decode("utf-8")
            return NodeAnswer(redact_credentials(text).text.encode("utf-8"), value_hash)
        attempt_receipt = attempt_receipts_by_execution.get(execution_value)
        if attempt_receipt is None:
            return None
        value_hash = Sha256Hash(str(attempt_receipt["value_hash"]))
        artifact_hash = attempt_receipt["artifact_hash"]
        if artifact_hash is None:
            return NodeAnswer(b"", value_hash)
        artifact = artifacts_by_hash.get(str(artifact_hash))
        if artifact is None:
            raise RuntimeError("output-schema refusal artifact is missing")
        text = artifact.content.decode("utf-8")
        return NodeAnswer(redact_credentials(text).text.encode("utf-8"), value_hash)

    results: dict[str, tuple[NodeAnswer | None, NodeAnswer | None]] = {}
    for run_id, execution in execution_by_run_id.items():
        answer = answers_by_execution.get(execution.value)
        results[run_id] = (answer, refusal_output_for(execution.value))
    return results


def _node_provenance(
    connection: Connection, execution_id: NodeExecutionId
) -> NodeProvenance | None:
    """Which agent produced this node's answer, as its receipt recorded it."""

    record = (
        connection.execute(
            sa.select(agent_receipts_v2).where(
                agent_receipts_v2.c.node_execution_id == execution_id.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    return NodeProvenance(
        role=str(record["role"]),
        provider_id=str(record["provider_id"]),
        model=str(record["model"]),
        executor_revision=str(record["executor_revision"]),
        executor_operational_identity=str(record["executor_operational_identity"]),
        auth_mode=str(record["auth_mode"]),
        profile_id=str(record["profile_id"]),
        agent_configuration_revision_hash=str(
            record["agent_configuration_revision_hash"]
        ),
        request_hash=str(record["request_hash"]),
        receipt_hash=str(record["receipt_hash"]),
    )


def _run_row_id(row: RunProjection | DefectiveRunProjection) -> RunId:
    """The run a listed row names, healthy or defective alike."""
    return row.run_id if isinstance(row, DefectiveRunProjection) else row.run.run_id


class DbosQueries:
    """Bounded SQLite projections; each call owns and closes its read connection."""

    def __init__(
        self,
        engine: Engine,
        projection_limit: DurableProjectionLimit,
        *,
        busy_timeout_seconds: float = 5.0,
        query_deadline_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not math.isfinite(busy_timeout_seconds)
            or not math.isfinite(query_deadline_seconds)
            or busy_timeout_seconds < 0.001
            or query_deadline_seconds <= 0
        ):
            raise ValueError(
                "query deadline must be finite and positive and SQLite busy timeout "
                "must be finite and at least one millisecond"
            )
        self._engine = engine
        self._projection_limit = projection_limit
        self._busy_timeout_milliseconds = int(busy_timeout_seconds * 1000)
        self._query_deadline_seconds = query_deadline_seconds
        self._monotonic = monotonic

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self._engine.connect() as connection:
            raw = connection.connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise TypeError("durable query adapter requires SQLite")
            original_busy_timeout = int(
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )
            try:
                connection.exec_driver_sql(
                    f"PRAGMA busy_timeout={self._busy_timeout_milliseconds}"
                )
                deadline = self._monotonic() + self._query_deadline_seconds
                raw.set_progress_handler(
                    lambda: int(self._monotonic() >= deadline), 1000
                )
                connection.exec_driver_sql("BEGIN DEFERRED")
                yield connection
            finally:
                try:
                    raw.set_progress_handler(None, 0)
                finally:
                    try:
                        connection.rollback()
                    finally:
                        connection.exec_driver_sql(
                            f"PRAGMA busy_timeout={original_busy_timeout}"
                        )

    def get_workflow_revision(
        self,
        revision_hash: WorkflowRevisionHash,
    ) -> GetWorkflowRevisionResult:
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        _bounded_projection_select(
                            workflow_revisions,
                            self._projection_limit,
                            document_columns=_REVISION_DOCUMENT_COLUMNS,
                        )
                        .add_columns(*_REVISION_PROVENANCE_COLUMNS)
                        .select_from(_workflow_revisions_with_provenance())
                        .where(
                            workflow_revisions.c.revision_hash == revision_hash.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return WorkflowRevisionMissing()
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    document_columns=_REVISION_DOCUMENT_COLUMNS,
                )
                document_bytes = bytes(record["document"])
                self._projection_limit.validate_document(document_bytes)
                revision = WorkflowRevision(document_bytes)
                if revision.revision_hash != revision_hash:
                    return QueryDurableStateCorrupt()
                graph = _parsed_workflow_revision(revision)
                self._projection_limit.validate_graph(graph)
                return WorkflowRevisionFound(
                    WorkflowRevisionProjection(revision, graph),
                    _revision_provenance(record),
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult:
        if (
            type(limit) is not int
            or not MINIMUM_PAGE_ITEMS <= limit <= MAXIMUM_PAGE_ITEMS
        ):
            raise ValueError(
                f"revision page limit must be an integer from {MINIMUM_PAGE_ITEMS} to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                statement = sa.select(workflow_revisions.c.revision_hash)
                if after is not None:
                    statement = statement.where(
                        workflow_revisions.c.revision_hash > after.value
                    )
                values = tuple(
                    WorkflowRevisionHash(str(value))
                    for value in connection.execute(
                        statement.order_by(workflow_revisions.c.revision_hash).limit(
                            limit + 1
                        )
                    ).scalars()
                )
                has_more = len(values) > limit
                items = values[:limit]
                return WorkflowRevisionPage(
                    items, items[-1] if has_more and items else None
                )
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_described_workflow_revisions(
        self,
        after: WorkflowRevisionHash | None,
        limit: int,
        budget: EnrichedPageBudget,
    ) -> ListDescribedWorkflowRevisionsResult:
        """One page of revisions with their documents, in one bounded query.

        The rows stream one at a time on purpose: a page that fetched its whole
        limit before spending its budget would move every document it then
        refused to use, which is the byte cost the budget exists to bound.
        """

        if (
            type(limit) is not int
            or not MINIMUM_PAGE_ITEMS <= limit <= MAXIMUM_PAGE_ITEMS
        ):
            raise ValueError(
                f"revision page limit must be an integer from {MINIMUM_PAGE_ITEMS} to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                statement = sa.select(
                    workflow_revisions.c.revision_hash,
                    workflow_revisions.c.document,
                    *_REVISION_PROVENANCE_COLUMNS,
                ).select_from(_workflow_revisions_with_provenance())
                if after is not None:
                    statement = statement.where(
                        workflow_revisions.c.revision_hash > after.value
                    )
                streamed = connection.execution_options(yield_per=1).execute(
                    statement.order_by(workflow_revisions.c.revision_hash).limit(
                        limit + 1
                    )
                )
                items: list[ListedWorkflowRevision] = []
                spent_nodes = 0
                spent_bytes = 0
                exhausted = False
                for record in streamed.mappings():
                    if len(items) == limit:
                        exhausted = True
                        break
                    document = bytes(record["document"])
                    if items and spent_bytes + len(document) > (
                        budget.maximum_document_bytes
                    ):
                        exhausted = True
                        break
                    self._projection_limit.validate_document(document)
                    revision = WorkflowRevision(document)
                    if revision.revision_hash.value != str(record["revision_hash"]):
                        return QueryDurableStateCorrupt()
                    graph = _parsed_workflow_revision(revision)
                    self._projection_limit.validate_graph(graph)
                    if items and spent_nodes + len(graph.nodes) > budget.maximum_nodes:
                        exhausted = True
                        break
                    items.append(
                        ListedWorkflowRevision(
                            WorkflowRevisionProjection(revision, graph),
                            _revision_provenance(record),
                        )
                    )
                    spent_bytes += len(document)
                    spent_nodes += len(graph.nodes)
                streamed.close()
                return DescribedWorkflowRevisionPage(
                    tuple(items),
                    items[-1].projection.revision.revision_hash
                    if exhausted and items
                    else None,
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_node_detail(self, run_id: RunId, node_id: str) -> GetNodeDetailResult:
        """One node of one run, answered from what the run really kept.

        An Agent job is composed again through the one owner that composed it
        for the provider. A no-input Wait reads its authored prompt, preserving
        its published-parse behavior. An input-bearing Wait that has paused
        reads the exact question from its immutable WAITING_INPUT event; only a
        live preview is composed. In each case the plain byte hash travels as
        job_hash. For an Agent, the hash a reader holds against the receipt is
        provenance.request_hash, which frames execution identity, revision,
        binding and operational identity around those bytes.

        A refusal has two durable voices. Every refused attempt writes its own
        immutable Attempt receipt; ordinal one also records a nonterminal event
        before the repair, while ordinal two additionally writes the terminal
        `failed` `node-receipt/v3`. The exact attempt receipt is the fallback
        when that terminal node receipt is absent. A run from before either
        record family stays honestly absent in those tables, so its refusal is
        still recomputed through the composition owner -- named either way, so
        the operator is told why a run stands still instead of watching it
        stand still.
        """

        found = self.get_run(run_id)
        if not isinstance(found, RunFound):
            return found
        projection = found.projection
        try:
            with self._connection() as connection:
                try:
                    node = projection.graph.node(node_id)
                except KeyError:
                    return NodeQueryMissing()
                rail = {
                    entry.node_id: entry.state
                    for entry in project_node_rail(projection, ())
                }
                if node_id not in rail:
                    return NodeQueryMissing()
                round_ordinal, execution_id = _node_detail_execution(
                    connection,
                    projection,
                    node_id,
                    node,
                    rail[node_id],
                )
                job, job_hash, refusal = _node_job_and_refusal(
                    connection, projection, node, round_ordinal, rail[node_id]
                )
                durable_refusal = _node_receipt_refusal(
                    connection, execution_id
                ) or _unavailable_executor_refusal(connection, execution_id)
                started_at, ended_at = _node_instants(connection, execution_id)
                named_refusal = durable_refusal
                if named_refusal is None:
                    named_refusal = refusal
                if named_refusal is None:
                    named_refusal = _abandoned_intent_refusal(projection, node_id)
                return NodeDetailFound(
                    NodeDetail(
                        run_id=run_id,
                        node_id=node_id,
                        state=rail[node_id],
                        job=job,
                        job_hash=job_hash,
                        answer=_node_answer(connection, node, execution_id),
                        provenance=_node_provenance(connection, execution_id),
                        refusal=named_refusal,
                        refusal_output=_node_receipt_refusal_output(
                            connection, execution_id
                        ),
                        started_at=started_at,
                        ended_at=ended_at,
                        transcript=_node_transcript(connection, execution_id),
                    )
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_run(
        self,
        run_id: RunId,
    ) -> GetRunResult:
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        _bounded_projection_select(
                            runs,
                            self._projection_limit,
                            columns=_RUN_PROJECTION_COLUMNS,
                            field_columns=_RUN_FIELD_COLUMNS,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return RunQueryMissing()
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    field_columns=_RUN_FIELD_COLUMNS,
                )
                return RunFound(self._run_projections(connection, (record,))[0])
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError) as error:
            _LOG.error(
                "run get projection failed for run_id=%s: %s",
                run_id.value,
                error,
                exc_info=error,
                extra={
                    "event": "run_get_projection_corrupt",
                    "run_id": run_id.value,
                },
            )
            return QueryDurableStateCorrupt()

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        state: RunState | None = None,
    ) -> ListRunsResult:
        if (
            type(limit) is not int
            or not MINIMUM_PAGE_ITEMS <= limit <= MAXIMUM_PAGE_ITEMS
        ):
            raise ValueError(
                f"run page limit must be an integer from {MINIMUM_PAGE_ITEMS} to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                statement = _bounded_projection_select(
                    runs,
                    self._projection_limit,
                    columns=_RUN_PROJECTION_COLUMNS,
                    field_columns=_RUN_FIELD_COLUMNS,
                )
                if after is not None:
                    statement = statement.where(runs.c.run_id > after.value)
                if state is not None:
                    statement = statement.where(runs.c.state == state.value)
                records = tuple(
                    connection.execute(
                        statement.order_by(runs.c.run_id).limit(limit + 1)
                    ).mappings()
                )
                has_more = len(records) > limit
                item_records = records[:limit]
                for record in item_records:
                    _validate_bounded_record(
                        record,
                        self._projection_limit,
                        field_columns=_RUN_FIELD_COLUMNS,
                    )
                ordered_bytes = tuple(
                    str(record["run_id"]).encode("utf-8") for record in item_records
                )
                if ordered_bytes != tuple(sorted(ordered_bytes)) or (
                    after is not None
                    and ordered_bytes
                    and ordered_bytes[0] <= after.value.encode("utf-8")
                ):
                    raise RunTransitionConflict(
                        "SQLite run order disagrees with exact UTF-8 byte order"
                    )
                rows = self._run_rows(connection, item_records)
                return RunPage(
                    rows,
                    (_run_row_id(rows[-1]) if has_more and rows else None),
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (UnicodeEncodeError, ValueError, RuntimeError, DatabaseError) as error:
            _LOG.error(
                "run list projection failed",
                exc_info=error,
                extra={"event": "run_list_projection_corrupt"},
            )
            return QueryDurableStateCorrupt()

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
    ) -> GetReconciliationRetryTargetResult:
        try:
            with self._connection() as connection:
                run_exists = connection.scalar(
                    sa.select(sa.literal(True)).where(runs.c.run_id == run_id.value)
                )
                if run_exists is None:
                    return RunQueryMissing()
                command_record = (
                    connection.execute(
                        _bounded_projection_select(
                            reconcile_commands,
                            self._projection_limit,
                            payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                            field_columns=_COMMAND_FIELD_COLUMNS,
                        ).where(reconcile_commands.c.command_id == command_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if command_record is None:
                    return ReconciliationRetryTargetMissing()
                _validate_bounded_record(
                    command_record,
                    self._projection_limit,
                    payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                    field_columns=_COMMAND_FIELD_COLUMNS,
                )
                intent_record = (
                    connection.execute(
                        _bounded_projection_select(
                            effect_intents,
                            self._projection_limit,
                            payload_columns=_INTENT_PAYLOAD_COLUMNS,
                            field_columns=_INTENT_FIELD_COLUMNS,
                        ).where(
                            effect_intents.c.logical_key
                            == command_record["logical_key"]
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if intent_record is None:
                    return QueryDurableStateCorrupt()
                _validate_bounded_record(
                    intent_record,
                    self._projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                )
                intent = intent_snapshot_from_record(intent_record)
                if intent.intent.binding.run_id != run_id:
                    return ReconciliationRetryCommandConflict()
                return ReconciliationRetryTargetFound(intent)
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def _run_rows(
        self,
        connection: Connection,
        records: Sequence[Mapping[Any, Any]],
    ) -> tuple[RunProjection | DefectiveRunProjection, ...]:
        """Every listed run's own projection, told apart from its neighbours.

        `_run_projections` joins the whole page in shared queries, so one run
        whose own projection cannot be told must not cost every healthy run
        beside it (#1042): a batch failure retries the page row by row, and
        only the row that still fails on its own becomes a defective row
        instead of taking the whole page down with it. `ProjectionLimitExceeded`
        is not a row's own defect but the read edge's admitted-size bound, so it
        is left to propagate to the page-level refusal `list_runs` already owns.
        """
        if not records:
            return ()
        try:
            return self._run_projections(connection, records)
        except (ProjectionLimitExceeded, OperationalError, PoolTimeoutError):
            raise
        except (UnicodeEncodeError, ValueError, RuntimeError, DatabaseError) as error:
            _LOG.error(
                "run list projection failed for the page; retrying its rows"
                " individually",
                exc_info=error,
                extra={"event": "run_list_projection_corrupt"},
            )
        rows: list[RunProjection | DefectiveRunProjection] = []
        for record in records:
            run_id = RunId(str(record["run_id"]))
            try:
                rows.append(self._run_projections(connection, (record,))[0])
            except (ProjectionLimitExceeded, OperationalError, PoolTimeoutError):
                raise
            except (
                UnicodeEncodeError,
                ValueError,
                RuntimeError,
                DatabaseError,
            ) as error:
                _LOG.error(
                    "run list projection failed for run_id=%s: %s",
                    run_id.value,
                    error,
                    exc_info=error,
                    extra={
                        "event": "run_list_projection_corrupt",
                        "run_id": run_id.value,
                    },
                )
                rows.append(
                    DefectiveRunProjection(
                        run_id,
                        RunProjectionProblemCode.DURABLE_STATE_CORRUPT,
                        bounded_run_row_defect_detail(error),
                    )
                )
        return tuple(rows)

    def _run_projections(
        self,
        connection: Connection,
        records: Sequence[Mapping[Any, Any]],
    ) -> tuple[RunProjection, ...]:
        if not records:
            return ()
        loaded_runs = runs_from_records_with_bindings(connection, records)
        run_ids = tuple(run.run_id.value for run in loaded_runs)
        successor_fork_records = tuple(
            connection.execute(
                _bounded_projection_select(
                    run_forks,
                    self._projection_limit,
                    field_columns=_RUN_FORK_FIELD_COLUMNS,
                ).where(run_forks.c.successor_run_id.in_(run_ids))
            ).mappings()
        )
        maximum_successor_records = len(run_ids) * MAXIMUM_RUN_FORK_SUCCESSORS
        origin_fork_records = tuple(
            connection.execute(
                _bounded_projection_select(
                    run_forks,
                    self._projection_limit,
                    field_columns=_RUN_FORK_FIELD_COLUMNS,
                )
                .where(run_forks.c.origin_run_id.in_(run_ids))
                .order_by(run_forks.c.origin_run_id, run_forks.c.successor_run_id)
                .limit(maximum_successor_records + 1)
            ).mappings()
        )
        if len(origin_fork_records) > maximum_successor_records:
            raise ProjectionLimitExceeded(
                "run fork successor projection exceeds its limit"
            )
        successor_counts: dict[str, int] = {}
        for record in origin_fork_records:
            origin_id = str(record["origin_run_id"])
            successor_counts[origin_id] = successor_counts.get(origin_id, 0) + 1
            if successor_counts[origin_id] > MAXIMUM_RUN_FORK_SUCCESSORS:
                raise ProjectionLimitExceeded(
                    "run fork successor projection exceeds its limit"
                )
        fork_records = tuple(
            {
                str(record["command_id"]): record
                for record in (*successor_fork_records, *origin_fork_records)
            }.values()
        )
        for fork_record in fork_records:
            _validate_bounded_record(
                fork_record,
                self._projection_limit,
                field_columns=_RUN_FORK_FIELD_COLUMNS,
            )
        stored_forks = []
        for record in fork_records:
            # A same-snapshot invariant check, not a live race: `record` was
            # read from `run_forks` under this call's one SQLite snapshot
            # (`_connection` opens one `BEGIN DEFERRED` transaction for the
            # whole read), and `_stored_fork_for_command` re-reads the exact
            # same table under that unchanged snapshot -- so this branch
            # should be unreachable today. It stays as a guard rather than an
            # assumption, and if that snapshot guarantee is ever weakened, the
            # honest response is retrying the read, not treating the row as
            # permanently corrupt: nothing here proves the fork is gone, only
            # that this one read could not see it.
            fork = _stored_fork_for_command(
                connection, RunForkCommandId(str(record["command_id"]))
            )
            if fork is None:
                raise RunTransitionConflict("run fork disappeared during projection")
            validate_stored_fork(connection, fork)
            stored_forks.append(fork)
        origin_by_successor = {
            fork.successor_run_id.value: RunForkOriginProjection(
                fork.origin_run_id,
                fork.origin_terminal_hash,
                fork.restart_from_node_id,
                fork.fork_hash,
            )
            for fork in stored_forks
            if fork.successor_run_id.value in run_ids
        }
        successors_by_origin: dict[str, list[RunForkSuccessorProjection]] = {}
        reused_by_successor: dict[str, list[ReusedNodeProjection]] = {}
        for fork in stored_forks:
            if fork.origin_run_id.value in run_ids:
                successors_by_origin.setdefault(fork.origin_run_id.value, []).append(
                    RunForkSuccessorProjection(
                        fork.successor_run_id,
                        fork.restart_from_node_id,
                        fork.fork_hash,
                    )
                )
            if fork.successor_run_id.value in run_ids:
                reused_by_successor[fork.successor_run_id.value] = [
                    ReusedNodeProjection(
                        entry.node_id,
                        entry.source_run_id,
                        entry.source_event_hash,
                        entry.source_receipt_hash,
                        entry.source_declared_context_package_hash,
                    )
                    for entry in fork.reused_nodes
                ]
        for successors in successors_by_origin.values():
            successors.sort(key=lambda item: item.successor_run_id.value.encode())
        revision_hashes = {run.revision_hash for run in loaded_runs}
        revision_rows = tuple(
            connection.execute(
                _bounded_projection_select(
                    workflow_revisions,
                    self._projection_limit,
                    document_columns=_REVISION_DOCUMENT_COLUMNS,
                ).where(
                    workflow_revisions.c.revision_hash.in_(
                        tuple(value.value for value in revision_hashes)
                    )
                )
            ).mappings()
        )
        for record in revision_rows:
            _validate_bounded_record(
                record,
                self._projection_limit,
                document_columns=_REVISION_DOCUMENT_COLUMNS,
            )
        revision_records = {
            WorkflowRevisionHash(str(record["revision_hash"])): bytes(
                record["document"]
            )
            for record in revision_rows
        }
        if set(revision_records) != revision_hashes:
            raise RunTransitionConflict(
                "run page references a missing workflow revision"
            )
        graphs = {}
        for revision_hash, document in revision_records.items():
            self._projection_limit.validate_document(document)
            stored = WorkflowRevision(document)
            if stored.revision_hash != revision_hash:
                raise RevisionHashCollision(
                    "durable workflow revision bytes disagree with their hash"
                )
            # A run already started against these bytes. Today's executable
            # parse may refuse the same document; listing and inspecting it
            # is a read of published history, not a start.
            graph = parse_workflow_document(document)
            self._projection_limit.validate_graph(graph)
            graphs[revision_hash] = graph
        for run in loaded_runs:
            validate_run_graph_binding(run, graphs[run.revision_hash])

        current_agent_executions = {
            run.run_id: _node_execution_id(
                run, graphs[run.revision_hash], run.current_node_id
            )
            for run in loaded_runs
            if isinstance(
                graphs[run.revision_hash].node(run.current_node_id),
                AgentNodeV3,
            )
        }
        attempt_records: dict[str, list[Mapping[Any, Any]]] = {}
        if current_agent_executions:
            for record in connection.execute(
                _bounded_projection_select(
                    agent_attempts,
                    self._projection_limit,
                    field_columns=_ATTEMPT_FIELD_COLUMNS,
                ).where(
                    agent_attempts.c.node_execution_id.in_(
                        tuple(
                            execution.value
                            for execution in current_agent_executions.values()
                        )
                    )
                )
            ).mappings():
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    field_columns=_ATTEMPT_FIELD_COLUMNS,
                )
                execution_value = str(record["node_execution_id"])
                attempt_records.setdefault(execution_value, []).append(record)
            for records_for_execution in attempt_records.values():
                records_for_execution.sort(
                    key=lambda item: int(item["attempt_ordinal"])
                )
                ordinals = tuple(
                    int(item["attempt_ordinal"]) for item in records_for_execution
                )
                if ordinals not in {(1,), (1, 2)}:
                    raise RunTransitionConflict(
                        "current node has a noncanonical agent-attempt sequence"
                    )

        waiting_runs = tuple(
            run for run in loaded_runs if run.state is RunState.WAITING_RECONCILIATION
        )
        ended_action_runs = tuple(
            run
            for run in loaded_runs
            if run.state in {RunState.FAILED, RunState.CANCELLED, RunState.COMPLETED}
            and isinstance(
                graphs[run.revision_hash].node(run.current_node_id),
                ActionNodeV3,
            )
        )
        intent_runs = waiting_runs + ended_action_runs
        logical_keys_by_run = {
            run.run_id: logical_effect_key_for_node(
                run.run_id,
                run.revision_hash,
                run.current_node_id,
                round_of(
                    graphs[run.revision_hash],
                    run.current_node_id,
                    run.current_round_ordinal,
                ),
            )
            for run in intent_runs
        }
        intent_records: dict[str, Mapping[Any, Any]] = {}
        if intent_runs:
            for record in connection.execute(
                _bounded_projection_select(
                    effect_intents,
                    self._projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                ).where(
                    effect_intents.c.logical_key.in_(
                        tuple(key.value for key in logical_keys_by_run.values())
                    )
                )
            ).mappings():
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                )
                key = str(record["logical_key"])
                if key in intent_records:
                    raise RunTransitionConflict("durable intent primary key repeated")
                intent_records[key] = record
        waiting_key_values = {
            logical_keys_by_run[run.run_id].value for run in waiting_runs
        }
        if waiting_key_values - set(intent_records):
            raise RunTransitionConflict(
                "WAITING_RECONCILIATION run has no exact durable intent"
            )

        owner_ids = tuple(
            str(record["reconciliation_owner_command_id"])
            for record in intent_records.values()
            if record["reconciliation_owner_command_id"] is not None
        )
        command_rows = (
            tuple(
                connection.execute(
                    _bounded_projection_select(
                        reconcile_commands,
                        self._projection_limit,
                        payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                        field_columns=_COMMAND_FIELD_COLUMNS,
                    ).where(reconcile_commands.c.command_id.in_(owner_ids))
                ).mappings()
            )
            if owner_ids
            else ()
        )
        for record in command_rows:
            _validate_bounded_record(
                record,
                self._projection_limit,
                payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                field_columns=_COMMAND_FIELD_COLUMNS,
            )
        command_records = {str(record["command_id"]): record for record in command_rows}
        if set(command_records) != set(owner_ids):
            raise RunTransitionConflict("reconciling intent command is missing")

        instants: dict[str, tuple[RecordedAt, RecordedAt | None]] = {}
        run_ids = tuple(run.run_id.value for run in loaded_runs)
        instant_rows = (
            connection.execute(
                sa.select(run_instants).where(run_instants.c.run_id.in_(run_ids))
            ).mappings()
            if run_ids
            else ()
        )
        for record in instant_rows:
            ended = record["ended_at"]
            instants[str(record["run_id"])] = (
                RecordedAt(str(record["started_at"])),
                None if ended is None else RecordedAt(str(ended)),
            )
        orders_by_run = load_run_orders(connection, run_ids)
        ended_v3_runs = tuple(
            run
            for run in loaded_runs
            if isinstance(run, RunV3) and run.terminal_hash is not None
        )
        terminal_results = _run_terminal_results(connection, ended_v3_runs, graphs)

        projections = []
        for run in loaded_runs:
            reconciliation: WaitingReconciliationProjection | None = None
            if run.state is RunState.WAITING_RECONCILIATION:
                logical_key = logical_keys_by_run[run.run_id]
                intent_record = intent_records[logical_key.value]
                intent = intent_snapshot_from_record(intent_record)
                if (
                    intent.intent.binding.run_id != run.run_id
                    or intent.intent.binding.workflow_revision_hash != run.revision_hash
                    or intent.intent.binding.logical_key != logical_key
                ):
                    raise RunTransitionConflict(
                        "waiting run intent binding disagrees with its logical key"
                    )
                pending = None
                owner = intent_record["reconciliation_owner_command_id"]
                if intent.state is EffectIntentState.RECONCILING:
                    if owner is None:
                        raise RunTransitionConflict(
                            "reconciling intent has no command owner"
                        )
                    pending = command_snapshot_from_record(
                        command_records[str(owner)], intent.intent
                    )
                    if pending.state is not ReconcileCommandState.PENDING:
                        raise RunTransitionConflict(
                            "reconciling intent command is not pending"
                        )
                elif (
                    intent.state is not EffectIntentState.WAITING_RECONCILIATION
                    or owner is not None
                ):
                    raise RunTransitionConflict(
                        "waiting reconciliation run has inconsistent intent state"
                    )
                reconciliation = WaitingReconciliationProjection(intent, pending)
            elif run.run_id in logical_keys_by_run:
                logical_key = logical_keys_by_run[run.run_id]
                intent_record = intent_records.get(logical_key.value)
                if intent_record is not None:
                    intent = intent_snapshot_from_record(intent_record)
                    if (
                        intent.intent.binding.run_id != run.run_id
                        or intent.intent.binding.workflow_revision_hash
                        != run.revision_hash
                        or intent.intent.binding.logical_key != logical_key
                    ):
                        raise RunTransitionConflict(
                            "ended run intent binding disagrees with its logical key"
                        )
                    if intent.state is EffectIntentState.ABANDONED:
                        if intent_record["reconciliation_owner_command_id"] is not None:
                            raise RunTransitionConflict(
                                "abandoned intent has a command owner"
                            )
                        reconciliation = WaitingReconciliationProjection(intent, None)
            attempt_projections: tuple[AgentAttemptProjection, ...] = ()
            execution = current_agent_executions.get(run.run_id)
            if execution is not None:
                if not isinstance(run, (RunV2, RunV3)):
                    raise RunTransitionConflict("agent node belongs to a V1 run")
                records_for_execution = attempt_records.get(execution.value, [])
                # A succeeded attempt has no public state unless the run
                # parks on its node's effect, so projecting it would refuse
                # the read. COMPLETED is that case. FAILED is not: the attempt
                # is still the current one, and the rail needs it so a list
                # read does not pose the node as working.
                # NEVER_LAUNCHED cleanup on a FAILED run is the exception: it
                # is control evidence for an attempt-less refusal, not the
                # public node ending.
                if records_for_execution and run.state is not RunState.COMPLETED:
                    graph = graphs[run.revision_hash]
                    attempt_projections = tuple(
                        _current_attempt_projection(
                            attempt_record,
                            session=connection,
                            run=run,
                            graph=graph,
                            effect_awaits_reconciliation=(
                                execution_awaits_effect_reconciliation(
                                    run.state, reconciliation, execution
                                )
                            ),
                        )
                        for attempt_record in records_for_execution
                    )
                    attempt_projections = tuple(
                        attempt
                        for attempt in attempt_projections
                        if not never_launched_cleanup_on_failed_run(run, attempt)
                    )
            instant = instants.get(run.run_id.value)
            terminal_answer, terminal_refusal_output = terminal_results.get(
                run.run_id.value, (None, None)
            )
            projections.append(
                RunProjection(
                    run,
                    graphs[run.revision_hash],
                    reconciliation,
                    attempt_projections,
                    None if instant is None else instant[0],
                    None if instant is None else instant[1],
                    orders=orders_by_run.get(run.run_id.value, ()),
                    fork_origin=origin_by_successor.get(run.run_id.value),
                    fork_successors=tuple(
                        successors_by_origin.get(run.run_id.value, ())
                    ),
                    reused_nodes=tuple(reused_by_successor.get(run.run_id.value, ())),
                    answer=terminal_answer,
                    refusal_output=terminal_refusal_output,
                )
            )
        return tuple(projections)

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        if type(after_sequence) is not int or after_sequence < 0:
            return EventHistoryCorrupt()
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        sa.select(
                            runs.c.state,
                            runs.c.last_event_sequence,
                            runs.c.workflow_format_version,
                            runs.c.revision_hash,
                            runs.c.current_node_id,
                            runs.c.current_round_ordinal,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return RunQueryMissing()
                head = int(record["last_event_sequence"])
                if after_sequence > head:
                    return CursorAhead()
                terminal = str(record["state"]) in {
                    RunState.COMPLETED.value,
                    RunState.FAILED.value,
                }
                if head == 0:
                    first_sequence = connection.scalar(
                        sa.select(run_events.c.event_sequence)
                        .where(run_events.c.run_id == run_id.value)
                        .order_by(run_events.c.event_sequence)
                        .limit(1)
                    )
                    if first_sequence is not None or terminal:
                        return EventHistoryCorrupt()
                    return StreamReady(head, terminal, after_sequence)

                required_sequences = {1, head}
                if after_sequence > 0:
                    required_sequences.add(after_sequence)
                endpoint_records = {
                    int(endpoint["event_sequence"]): endpoint
                    for endpoint in connection.execute(
                        sa.select(
                            run_events.c.event_sequence,
                            run_events.c.event_kind,
                            run_events.c.payload,
                            run_events.c.run_id,
                            run_events.c.revision_hash,
                            run_events.c.node_id,
                            run_events.c.node_execution_id,
                            run_events.c.round_ordinal,
                            run_events.c.agent_attempt_id,
                            run_events.c.attempt_ordinal,
                        ).where(
                            run_events.c.run_id == run_id.value,
                            run_events.c.event_sequence.in_(required_sequences),
                        )
                    ).mappings()
                }
                if set(endpoint_records) != required_sequences:
                    return EventHistoryCorrupt()
                ended_the_run = _run_ending_event_predicate(
                    NodeExecutionId.for_node(
                        run_id,
                        WorkflowRevisionHash(str(record["revision_hash"])),
                        str(record["current_node_id"]),
                        int(record["current_round_ordinal"]),
                    ),
                )
                if ended_the_run(connection, endpoint_records[head]) != terminal or any(
                    sequence < head and ended_the_run(connection, endpoint)
                    for sequence, endpoint in endpoint_records.items()
                ):
                    return EventHistoryCorrupt()
                return StreamReady(head, terminal, after_sequence)
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (TypeError, ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def read_run_event_page(
        self,
        run_id: RunId,
        after_sequence: int,
        limit: int,
    ) -> ReadRunEventPageResult:
        if (
            type(limit) is not int
            or not MINIMUM_PAGE_ITEMS <= limit <= MAXIMUM_PAGE_ITEMS
        ):
            raise ValueError(
                f"event page limit must be an integer from {MINIMUM_PAGE_ITEMS} to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                run_record = (
                    connection.execute(
                        sa.select(
                            runs.c.state,
                            runs.c.last_event_sequence,
                            runs.c.workflow_format_version,
                            runs.c.revision_hash,
                            runs.c.current_node_id,
                            runs.c.current_round_ordinal,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if run_record is None:
                    return QueryDurableStateCorrupt()
                head = int(run_record["last_event_sequence"])
                if after_sequence < 0 or after_sequence > head:
                    return EventHistoryCorrupt()
                records = tuple(
                    connection.execute(
                        _bounded_projection_select(
                            run_events,
                            self._projection_limit,
                            payload_columns=_EVENT_PAYLOAD_COLUMNS,
                            field_columns=_EVENT_FIELD_COLUMNS,
                        )
                        .where(
                            run_events.c.run_id == run_id.value,
                            run_events.c.event_sequence > after_sequence,
                        )
                        .order_by(run_events.c.event_sequence)
                        .limit(limit)
                    )
                    .mappings()
                    .all()
                )
                for record in records:
                    _validate_bounded_record(
                        record,
                        self._projection_limit,
                        payload_columns=_EVENT_PAYLOAD_COLUMNS,
                        field_columns=_EVENT_FIELD_COLUMNS,
                    )
                sequences = tuple(int(record["event_sequence"]) for record in records)
                expected_sequences = tuple(
                    range(after_sequence + 1, min(head, after_sequence + limit) + 1)
                )
                if sequences != expected_sequences:
                    return EventHistoryCorrupt()
                ended_the_run = _run_ending_event_predicate(
                    NodeExecutionId.for_node(
                        run_id,
                        WorkflowRevisionHash(str(run_record["revision_hash"])),
                        str(run_record["current_node_id"]),
                        int(run_record["current_round_ordinal"]),
                    ),
                )
                terminal_sequences = tuple(
                    int(record["event_sequence"])
                    for record in records
                    if ended_the_run(connection, record)
                )
                terminal = str(run_record["state"]) in {
                    RunState.COMPLETED.value,
                    RunState.FAILED.value,
                }
                reached_head = bool(sequences) and sequences[-1] == head
                if terminal_sequences not in ((), (head,)) or (
                    reached_head and ((terminal_sequences == (head,)) != terminal)
                ):
                    return EventHistoryCorrupt()
                events = tuple(
                    self._event_projection(
                        connection,
                        record,
                        WorkflowFormatVersion(
                            int(run_record["workflow_format_version"])
                        ),
                        self._projection_limit,
                    )
                    for record in records
                )
                return RunEventPage(
                    events,
                    terminal_sequences == (head,),
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (
            RevisionHashCollision,
            RunTransitionConflict,
            TypeError,
            ValueError,
            RuntimeError,
            DatabaseError,
        ):
            return QueryDurableStateCorrupt()

    def read_attention_event_page(
        self,
        after_run_id: RunId | None,
        after_sequence: int | None,
        limit: int,
        excluded_identities: tuple[tuple[RunId, int], ...],
    ) -> ReadAttentionEventPageResult:
        if (
            type(limit) is not int
            or not MINIMUM_PAGE_ITEMS <= limit <= MAXIMUM_PAGE_ITEMS
        ):
            raise ValueError(
                f"event page limit must be an integer from {MINIMUM_PAGE_ITEMS} to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                return load_attention_event_page(
                    connection,
                    after_run_id,
                    after_sequence,
                    limit,
                    self._projection_limit,
                    self._event_projection,
                    excluded_identities,
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (
            RevisionHashCollision,
            RunTransitionConflict,
            TypeError,
            ValueError,
            RuntimeError,
            DatabaseError,
        ):
            return QueryDurableStateCorrupt()

    @staticmethod
    def _event_projection(
        connection: Connection,
        record: Mapping[Any, Any],
        workflow_format_version: WorkflowFormatVersion,
        projection_limit: DurableProjectionLimit,
    ) -> PersistedRunEvent:
        event = event_from_record(record)
        wait_answer_actor: WaitAnswerAttribution | None = None
        if (
            workflow_format_version is WorkflowFormatVersion.V3
            and event.event_kind is RunEventKind.WAIT_ANSWERED
        ):
            answer_records = tuple(
                connection.execute(
                    sa.select(wait_answers).where(
                        wait_answers.c.node_execution_id
                        == event.node_execution_id.value
                    )
                ).mappings()
            )
            if len(answer_records) != 1:
                raise WaitAnswerProjectionCorrupt(
                    "wait answer event has no unique durable answer"
                )
            answer_record = answer_records[0]
            if (
                answer_record["actor"] is None
                and answer_record["actor_attribution_kind"]
                != WaitAnswerAttributionKind.LEGACY_UNATTRIBUTED.value
            ):
                raise WaitAnswerProjectionCorrupt(
                    "wait answer event has no durable actor"
                )
            try:
                answer_snapshot = wait_answer_snapshot_from_record(answer_record)
            except (RunTransitionConflict, TypeError, ValueError) as error:
                raise WaitAnswerProjectionCorrupt(
                    "wait answer event has an unreadable durable answer"
                ) from error
            answer = answer_snapshot.answer
            if (
                answer_snapshot.state is not WaitAnswerState.APPLIED
                or answer_snapshot.state_version != 1
                or answer.run_id != event.run_id
                or answer.revision_hash != event.revision_hash
                or answer.node_id != event.node_id
                or answer.node_execution_id != event.node_execution_id
                or answer.round_ordinal != event.round_ordinal
                or answer.answer_bytes != event.payload
                or answer.answer_hash != event.payload_hash
            ):
                raise WaitAnswerProjectionCorrupt(
                    "wait answer event and durable answer disagree"
                )
            wait_answer_actor = answer.actor
        if (
            event.event_kind is RunEventKind.AGENT_FAILED
            and workflow_format_version not in _AGENT_FAILURE_FORMATS
        ):
            raise RunTransitionConflict("V1 run carries an agent failure event")
        if event.event_kind is RunEventKind.AGENT_FAILED and event.payload not in {
            *(code.value.encode("ascii") for code in AgentAttemptFailureCode),
            AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii"),
        }:
            raise RunTransitionConflict("agent failure event payload is not canonical")
        node_receipt_reason = (
            AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
            if event.event_kind is RunEventKind.AGENT_FAILED
            and event.payload
            == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii")
            else (
                _node_receipt_refusal(
                    connection,
                    event.node_execution_id,
                    (
                        None
                        if event.attempt_binding is None
                        else event.attempt_binding.attempt_id
                    ),
                )
                if event.event_kind is RunEventKind.AGENT_FAILED
                else None
            )
        )
        if event.event_kind not in {
            RunEventKind.ACTION_RECONCILIATION_RESOLVED,
            RunEventKind.ACTION_COMPLETED,
        }:
            return PersistedRunEvent(
                event,
                None,
                workflow_format_version,
                node_receipt_reason,
                wait_answer_actor,
            )
        logical_key = event.receipt_logical_key
        if logical_key is None:
            raise RunTransitionConflict("receipt event has no logical key")
        receipt_record = (
            connection.execute(
                _bounded_projection_select(
                    effect_receipts,
                    projection_limit,
                    payload_columns=_RECEIPT_PAYLOAD_COLUMNS,
                    field_columns=_RECEIPT_FIELD_COLUMNS,
                ).where(effect_receipts.c.logical_key == logical_key.value)
            )
            .mappings()
            .one_or_none()
        )
        if receipt_record is None:
            raise RunTransitionConflict("receipt event has no durable receipt")
        _validate_bounded_record(
            receipt_record,
            projection_limit,
            payload_columns=_RECEIPT_PAYLOAD_COLUMNS,
            field_columns=_RECEIPT_FIELD_COLUMNS,
        )
        receipt = receipt_from_record(receipt_record)
        if (
            receipt.intent.binding.run_id != event.run_id
            or receipt.intent.binding.workflow_revision_hash != event.revision_hash
            or receipt.result.payload_hash != event.receipt_result_hash
        ):
            raise RunTransitionConflict("receipt event binding disagrees")
        return PersistedRunEvent(
            event,
            receipt,
            workflow_format_version,
            wait_answer_actor=wait_answer_actor,
        )
