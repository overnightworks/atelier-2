from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, assert_never

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.agent_effect_grants import (
    agent_node_redeems_platform_effect,
)
from atelier2.adapters.dbos.artifact_store import keep_artifact, read_stored_artifact
from atelier2.adapters.dbos.effect_store import (
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.instants import record_attempt_ended, record_attempt_started
from atelier2.adapters.dbos.names import (
    CANCELLATION_WORKFLOW_NAME,
    QUEUE_NAME,
    REPLACEMENT_WORKFLOW_NAME,
)
from atelier2.adapters.dbos.node_records import keep_node_receipt
from atelier2.adapters.dbos.produced_node_values import (
    NoProducibleValue,
    declared_output_schema_document,
    declared_output_schema_refusal,
    schema_refusal_receipt_reason,
    the_value_this_execution_produced,
)
from atelier2.adapters.dbos.run_store import (
    AgentReceiptConflict,
    ToolRedemptionConflict,
    _agent_receipt_v2_from_record,
    _agent_receipt_v2_values,
    _tool_redemption_from_record,
    _tool_redemption_values,
    load_kept_value,
    load_node_outputs,
    load_run_inputs,
)
from atelier2.adapters.dbos.run_transitions import (
    RunPosition,
    RunTransitionConflict,
    _commit_event,
    _insert_event,
    commit_wait_cancelled,
    lift_started_run,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    agent_receipts_v2,
    effect_intents,
    effect_receipts,
    permission_receipts,
    run_events,
    runs,
    tool_redemptions,
    wait_answers,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.uncontinuable_runs import live_driver_workflow_ids
from atelier2.adapters.dbos.verification_failure_words import (
    verification_failure_verdict,
)
from atelier2.adapters.dbos.workflow_ids import (
    cancellation_workflow_id_for,
    driving_workflow_ids,
    replacement_workflow_id_for,
)
from atelier2.application.compose_node_job import (
    NodeJobCompositionVersion,
    OutputSchemaRepair,
    node_job,
)
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    OutputSchemaRefusalReceipt,
    RunnerEvidenceAcceptancePhase,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerTerminalEvidenceHash,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_permissions import (
    PermissionAuthority,
    PermissionCorrelationId,
    PermissionEffect,
    PermissionPolicyRevisionHash,
    PermissionReceipt,
    PermissionScope,
    PermissionScopeKind,
)
from atelier2.contracts.agent_refusals import (
    AGENT_REFUSAL_SCHEMA,
    agent_refusal_reason,
)
from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
    AgentReceiptV2,
)
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.contracts.effects import EffectIntentState
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    AgentExecutionRefusal,
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    WaitAnswerState,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    NodeArtifact,
    NodeReceiptReason,
    PersistedReceiptDisposition,
    node_receipt_reason,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.process_endings import (
    ProcessExitSignature,
    process_exit_verdict,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import AnyRun, RunV2, RunV3
from atelier2.contracts.run_cancellations import (
    CancelRunRequest,
    RunCancelCommandId,
    is_operator_run_cancel,
)
from atelier2.contracts.run_projections import RunCancellationRefusal
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import (
    ToolRedemptionReceipt,
)
from atelier2.contracts.verdicts import Verdict, read_verdict
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows import (
    NodeCompletion,
    RunCompletes,
    RunContinues,
    completion_after_node,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    NodeOutput,
    WorkflowGraphV3,
    verdict_condition_of,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationCommandConflict,
    AgentAttemptCancellationNotCurrent,
    AgentAttemptCancellationResult,
    AgentAttemptCancellationRunMissing,
    AgentAttemptCancellationStale,
    AgentAttemptCancellationTargetMissing,
    AgentAttemptCancellationTerminalConflict,
    AgentAttemptClaimedByThisCall,
    AgentAttemptClaimResult,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptReplacementNotAllowed,
    AgentAttemptSucceeded,
    AgentExecutorBindingRefusalFenced,
    AgentExecutorBindingRefusalNeedsPreparedCleanup,
    AgentExecutorBindingRefusalResult,
    AgentExecutorBindingRefusalWritten,
    ProjectVerificationFailureEvidence,
    RunCancellationAccepted,
    RunCancellationCommandConflict,
    RunCancellationEndedRun,
    RunCancellationNotCancellable,
    RunCancellationOvertakenBySuccess,
    RunCancellationResult,
    RunCancellationRunMissing,
    RunCancellationTerminalRetry,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt


class PermissionReceiptConflict(RunTransitionConflict):
    """One question of one attempt contradicts its durable permission receipt."""


class DurableStateCorrupt(RuntimeError):
    """A durable row disagrees with a value only this store could have written.

    Distinct from `PermissionReceiptConflict`: a conflict is two honest answers
    to the same question, and it names a decision this store must reconcile. A
    row whose stored hash no longer matches what its own content re-derives
    could not have come from this store's write at all, so there is no decision
    to reconcile -- the durable state itself is no longer trusted.
    """


_OR_IGNORE = "OR IGNORE"


def _cancellation_disposition(
    value: object,
) -> AgentAttemptCancellationDisposition | None:
    return None if value is None else AgentAttemptCancellationDisposition(str(value))


def attempt_from_record(record: Mapping[Any, Any]) -> AgentAttempt:
    """Rebuild the typed attempt one `agent_attempts` row records.

    This module owns the row-to-attempt mapping, so a second reader that needs an
    attempt back from its durable row -- the live-GitHub startup scan asking which
    workflow still drives it -- reads it here rather than re-deriving the shape.
    """
    try:
        failure = record["failure_code"]
        receipt = record["receipt_hash"]
        owner = record["process_owner_id"]
        generation = record["watchdog_generation_id"]
        command_id = record["cancellation_command_id"]
        disposition = record["cancellation_disposition"]
        runner_manifest = record["runner_manifest_id"]
        runner_generation = record["runner_generation_id"]
        runner_invocation = record["runner_invocation_id"]
        runner_evidence_hash = record["runner_terminal_evidence_hash"]
        transcript = record["transcript_artifact_hash"]
        disposition_value = _cancellation_disposition(disposition)
        cancellation = (
            None
            if command_id is None
            else AgentAttemptCancellation(
                str(command_id),
                int(record["cancellation_expected_state_version"]),
                AgentAttemptReplacement(str(record["replacement"])),
                AgentAttemptRedriveState(str(record["redrive_state"])),
                disposition_value,
            )
        )
        return AgentAttempt(
            AgentAttemptId(str(record["attempt_id"])),
            NodeExecutionId(str(record["node_execution_id"])),
            AgentExecutionRequestHash(str(record["request_hash"])),
            AgentExecutorOperationalIdentity(
                str(record["executor_operational_identity"])
            ),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            str(record["node_id"]),
            int(record["attempt_ordinal"]),
            AgentAttemptState(str(record["state"])),
            int(record["state_version"]),
            None if failure is None else AgentAttemptFailureCode(str(failure)),
            None if receipt is None else AgentReceiptHash(str(receipt)),
            AgentAttemptProcessPhase(str(record["process_phase"])),
            None if owner is None else AgentProcessOwnerId(str(owner)),
            None if generation is None else WatchdogGenerationId(str(generation)),
            cancellation,
            None if runner_manifest is None else RunnerManifestId(str(runner_manifest)),
            None
            if runner_generation is None
            else RunnerGenerationId(str(runner_generation)),
            None
            if runner_invocation is None
            else RunnerInvocationId(str(runner_invocation)),
            None
            if runner_evidence_hash is None
            else RunnerTerminalEvidenceHash(str(runner_evidence_hash)),
            RunnerEvidenceAcceptancePhase(
                str(record["runner_evidence_acceptance_phase"])
            ),
            None if transcript is None else ArtifactHash(str(transcript)),
        )
    except (TypeError, ValueError) as error:
        raise RunTransitionConflict(
            "durable agent attempt binding disagrees"
        ) from error


def _attempt_values(attempt: AgentAttempt) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id.value,
        "node_execution_id": attempt.node_execution_id.value,
        "request_hash": attempt.request_hash.value,
        "executor_operational_identity": attempt.executor_operational_identity.value,
        "run_id": attempt.run_id.value,
        "workflow_revision_hash": attempt.workflow_revision_hash.value,
        "node_id": attempt.node_id,
        "attempt_ordinal": attempt.attempt_ordinal,
        "state": attempt.state.value,
        "state_version": attempt.state_version,
        "process_phase": attempt.process_phase.value,
        "process_owner_id": (
            None if attempt.process_owner_id is None else attempt.process_owner_id.value
        ),
        "watchdog_generation_id": (
            None
            if attempt.watchdog_generation_id is None
            else attempt.watchdog_generation_id.value
        ),
        "cancellation_command_id": (
            None if attempt.cancellation is None else attempt.cancellation.command_id
        ),
        "cancellation_expected_state_version": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.expected_attempt_state_version
        ),
        "replacement": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.replacement.value
        ),
        "redrive_state": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.redrive_state.value
        ),
        "cancellation_disposition": (
            None
            if attempt.cancellation is None or attempt.cancellation.disposition is None
            else attempt.cancellation.disposition.value
        ),
        "cancellation_workflow_id": None,
        "failure_code": (
            None if attempt.failure_code is None else attempt.failure_code.value
        ),
        "receipt_hash": (
            None if attempt.receipt_hash is None else attempt.receipt_hash.value
        ),
        "runner_manifest_id": (
            None
            if attempt.runner_manifest_id is None
            else attempt.runner_manifest_id.value
        ),
        "runner_generation_id": (
            None
            if attempt.runner_generation_id is None
            else attempt.runner_generation_id.value
        ),
        "runner_invocation_id": (
            None
            if attempt.runner_invocation_id is None
            else attempt.runner_invocation_id.value
        ),
        "runner_terminal_evidence_hash": (
            None
            if attempt.runner_terminal_evidence_hash is None
            else attempt.runner_terminal_evidence_hash.value
        ),
        "runner_evidence_acceptance_phase": (
            attempt.runner_evidence_acceptance_phase.value
        ),
        "transcript_artifact_hash": (
            None
            if attempt.transcript_artifact_hash is None
            else attempt.transcript_artifact_hash.value
        ),
    }


def _prepared_attempt(execution: AgentAttemptExecution) -> AgentAttempt:
    request = execution.request
    return AgentAttempt(
        execution.attempt_id,
        request.node_execution_id,
        request.request_hash,
        request.executor_operational_identity,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.ordinal,
        AgentAttemptState.PREPARED,
        0,
    )


def _load_attempt(session: Any, attempt_id: AgentAttemptId) -> AgentAttempt:
    record = (
        session.execute(
            sa.select(agent_attempts).where(
                agent_attempts.c.attempt_id == attempt_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("agent attempt is missing")
    return attempt_from_record(record)


def _agent_node_for_attempt(graph: WorkflowGraphV3, node_id: str) -> AgentNodeV3:
    """Return the declared Agent node an attempt is allowed to name."""
    node = graph.node(node_id)
    if not isinstance(node, AgentNodeV3):
        raise RunTransitionConflict("agent attempt request differs from durable graph")
    return node


def load_output_schema_refusal_receipt(
    connection: Any,
    attempt_id: AgentAttemptId,
    *,
    expected_node_execution_id: NodeExecutionId,
    expected_attempt_ordinal: int,
    expected_schema_revision: PublishedRevisionHash,
) -> OutputSchemaRefusalReceipt | None:
    """Load one immutable refusal row and prove every identity it carries.

    A receipt is permission to compose the repair request.  Returning a partly
    checked row would turn a loose reason string back into that permission, so
    this owner validates the attempt it belongs to, the schema and bytes it
    judged, and the receipt hash derived from the complete row.
    """
    attempt = (
        connection.execute(
            sa.select(agent_attempts).where(
                agent_attempts.c.attempt_id == attempt_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if attempt is None:
        raise RunTransitionConflict("output-schema refusal attempt is missing")
    if (
        str(attempt["attempt_id"]) != attempt_id.value
        or str(attempt["node_execution_id"]) != expected_node_execution_id.value
        or int(attempt["attempt_ordinal"]) != expected_attempt_ordinal
    ):
        raise RunTransitionConflict(
            "output-schema refusal receipt belongs to another attempt"
        )
    record = (
        connection.execute(
            sa.select(agent_attempt_receipts_v3).where(
                agent_attempt_receipts_v3.c.attempt_id == attempt_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    receipt = OutputSchemaRefusalReceipt(
        AgentAttemptId(str(record["attempt_id"])),
        str(record["reason"]),
        PublishedRevisionHash(str(record["schema_revision_hash"])),
        Sha256Hash(str(record["value_hash"])),
        (
            None
            if record["artifact_hash"] is None
            else ArtifactHash(str(record["artifact_hash"]))
        ),
    )
    if (
        receipt.attempt_id != attempt_id
        or receipt.schema_revision != expected_schema_revision
        or receipt.receipt_hash.value != str(record["receipt_hash"])
    ):
        raise RunTransitionConflict("output-schema refusal receipt binding differs")
    _require_refusal_value_stored(connection, receipt)
    return receipt


def _require_refusal_value_stored(
    connection: Connection, receipt: OutputSchemaRefusalReceipt
) -> None:
    """The bytes the refusal judged are on hand, or the refusal judged none."""
    if receipt.artifact_hash is None:
        if receipt.value_hash != Sha256Hash.of(b""):
            raise RunTransitionConflict(
                "nonempty output-schema refusal has no artifact"
            )
        return
    if receipt.artifact_hash.value != receipt.value_hash.value:
        raise RunTransitionConflict(
            "output-schema refusal artifact differs from its value hash"
        )
    artifact = read_stored_artifact(connection, receipt.artifact_hash)
    if artifact is None or Sha256Hash.of(artifact.content) != receipt.value_hash:
        raise RunTransitionConflict(
            "output-schema refusal artifact is missing or differs"
        )


def load_prior_output_schema_refusal_receipt(
    connection: Any,
    *,
    target_attempt_id: AgentAttemptId,
    target_node_execution_id: NodeExecutionId,
    target_attempt_ordinal: int,
    expected_schema_revision: PublishedRevisionHash,
) -> OutputSchemaRefusalReceipt | None:
    """The exact ordinal-one receipt strictly before a repair candidate."""
    if target_attempt_ordinal == AGENT_ATTEMPT_ORDINAL:
        return None
    if target_attempt_ordinal != REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
        raise RunTransitionConflict("repair target ordinal is outside the vocabulary")
    prior_attempt_id = connection.scalar(
        sa.select(agent_attempts.c.attempt_id).where(
            agent_attempts.c.node_execution_id == target_node_execution_id.value,
            agent_attempts.c.attempt_ordinal == AGENT_ATTEMPT_ORDINAL,
        )
    )
    if prior_attempt_id is None:
        return None
    receipt = load_output_schema_refusal_receipt(
        connection,
        AgentAttemptId(str(prior_attempt_id)),
        expected_node_execution_id=target_node_execution_id,
        expected_attempt_ordinal=AGENT_ATTEMPT_ORDINAL,
        expected_schema_revision=expected_schema_revision,
    )
    if receipt is not None and receipt.attempt_id == target_attempt_id:
        raise RunTransitionConflict("repair receipt is not strictly prior")
    return receipt


def compose_agent_node_job_for_attempt(
    node: AgentNodeV3,
    orders: tuple[Any, ...],
    results: tuple[Any, ...],
    *,
    target_node_execution_id: NodeExecutionId,
    target_attempt_ordinal: int,
    prior_refusal_receipt: OutputSchemaRefusalReceipt | None,
) -> bytes:
    """Compose one attempt, repaired under its prior refusal receipt when one exists.

    Every attempt renders under `NodeJobCompositionVersion.CURRENT`, repaired
    to `OUTPUT_SCHEMA_REPAIR` exactly where a fully validated prior refusal
    receipt names the repair -- the base version is no longer a caller's
    choice (#1091 retired the `LEGACY` rendering rule it once selected
    between).
    """
    if prior_refusal_receipt is not None:
        if target_attempt_ordinal != REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
            raise RunTransitionConflict("repair receipt is not prior to its target")
        composition_version = NodeJobCompositionVersion.OUTPUT_SCHEMA_REPAIR
        repair = OutputSchemaRepair(prior_refusal_receipt.reason)
    else:
        composition_version = NodeJobCompositionVersion.CURRENT
        repair = None
    if not isinstance(target_node_execution_id, NodeExecutionId):
        raise TypeError("attempt composition requires a typed execution identity")
    return node_job(
        node.instruction,
        orders,
        results,
        composition_version=composition_version,
        output_schema_repair=repair,
    ).encode("utf-8")


def _validate_request(
    session: Any,
    request: AgentExecutionRequestV2,
    target_attempt_id: AgentAttemptId,
    target_attempt_ordinal: int,
) -> tuple[RunV2 | RunV3, WorkflowGraphV3]:
    """The run and graph one attempt request must exactly describe.

    A V3 agent node runs here too. Its role binding, its attempt identity and its
    provider contract are the ones V2 already carries, so admitting it is a wider
    door rather than a second path -- what a V3 run does differently begins after
    the provider answers, where the receipt chain is written.

    Only bound runs pass: a V1 run has no role matrix to attempt against. The
    graph is what decides how a node is read, and the run head is what decides
    where the run stands, so neither format is asked to answer for the other.
    """
    run = load_run(session, request.run_id)
    graph = load_graph(session, request.workflow_revision_hash)
    if not isinstance(run, (RunV2, RunV3)) or not isinstance(graph, WorkflowGraphV3):
        raise RunTransitionConflict("agent attempt requires a bound run")
    if run.revision_hash != request.workflow_revision_hash:
        raise RunTransitionConflict("agent attempt request names another revision")
    node = _agent_node_for_attempt(graph, request.node_id)
    # Recomputed from durable truth rather than trusted: what the node's author
    # wrote, plus the orders this run was started with, through the one owner
    # that decides what an agent is handed. A second spelling here would let a
    # request claim a job the document and the run never agreed on.
    schema_revision = PublishedRevisionHash(node.outputs[0].schema_reference.revision)
    prior_receipt = load_prior_output_schema_refusal_receipt(
        session,
        target_attempt_id=target_attempt_id,
        target_node_execution_id=request.node_execution_id,
        target_attempt_ordinal=target_attempt_ordinal,
        expected_schema_revision=schema_revision,
    )
    orders = load_run_inputs(session, request.run_id, node)
    results = load_node_outputs(
        session,
        request.run_id,
        request.workflow_revision_hash,
        graph,
        node,
        request.round_ordinal,
    )
    authored_job = compose_agent_node_job_for_attempt(
        node,
        orders,
        results,
        target_node_execution_id=request.node_execution_id,
        target_attempt_ordinal=target_attempt_ordinal,
        prior_refusal_receipt=prior_receipt,
    )
    if (
        node.role != request.resolved_binding.role.value
        or authored_job != request.job_bytes
    ):
        raise RunTransitionConflict("agent attempt request differs from durable graph")
    durable_binding = next(
        (
            binding
            for binding in run.agent_bindings
            if binding.role == request.resolved_binding.role
        ),
        None,
    )
    if durable_binding != request.resolved_binding:
        raise RunTransitionConflict(
            "agent attempt request differs from durable binding"
        )
    return run, graph


def _output_schema_repair_request(
    connection: Any,
    request: AgentExecutionRequestV2,
    graph: WorkflowGraphV3,
    node: AgentNodeV3,
    prior_receipt: OutputSchemaRefusalReceipt,
) -> AgentExecutionRequestV2:
    target_ordinal = REPLACEMENT_AGENT_ATTEMPT_ORDINAL
    job = compose_agent_node_job_for_attempt(
        node,
        load_run_inputs(connection, request.run_id, node),
        load_node_outputs(
            connection,
            request.run_id,
            request.workflow_revision_hash,
            graph,
            node,
            request.round_ordinal,
        ),
        target_node_execution_id=request.node_execution_id,
        target_attempt_ordinal=target_ordinal,
        prior_refusal_receipt=prior_receipt,
    )
    return AgentExecutionRequestV2(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        request.resolved_binding,
        request.executor_operational_identity,
        job,
        request.declared_output_schema_bytes,
        request.round_ordinal,
        request.maximum_assistant_turns,
    )


def _require_attempt_binding(
    attempt: AgentAttempt, execution: AgentAttemptExecution
) -> None:
    request = execution.request
    if (
        attempt.attempt_id != execution.attempt_id
        or attempt.node_execution_id != request.node_execution_id
        or attempt.request_hash != request.request_hash
        or attempt.executor_operational_identity
        != request.executor_operational_identity
        or attempt.run_id != request.run_id
        or attempt.workflow_revision_hash != request.workflow_revision_hash
        or attempt.node_id != request.node_id
        or attempt.attempt_ordinal != execution.ordinal
    ):
        raise RunTransitionConflict("durable agent attempt differs from exact retry")


def _require_completed_attempt_head(
    connection: Any,
    run: RunV2 | RunV3,
    request: AgentExecutionRequestV2,
    completion: NodeCompletion,
    completion_is_deferred: bool,
) -> None:
    if completion_is_deferred:
        if (
            run.state is not RunState.STARTED
            or run.current_node_id != request.node_id
            or run.current_round_ordinal != request.round_ordinal
        ):
            _require_confirmed_effect_receipt_for_completed_attempt(connection, request)
        else:
            return
    match completion:
        case RunContinues(node_id, round_ordinal):
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != node_id
                or run.current_round_ordinal != round_ordinal
            ):
                raise RunTransitionConflict(
                    "successful attempt has no exact successor transition"
                )
        case RunCompletes():
            if (
                run.state is not RunState.COMPLETED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict(
                    "successful terminal attempt has no exact completed transition"
                )
        case _ as unreachable:
            assert_never(unreachable)


def _require_confirmed_effect_receipt_for_completed_attempt(
    connection: Any, request: AgentExecutionRequestV2
) -> None:
    """Prove that this exact effect grant settled before accepting its replay.

    An attempt whose effect continuation already moved the run is safe to replay
    only when the intent for this execution reached CONFIRMED and its receipt is
    present. The intent also has to carry this attempt's durable output, otherwise
    an unrelated confirmed effect could make a torn head look like a continuation.
    """
    logical_key = logical_effect_key_for_node(
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        request.round_ordinal,
    )
    intent_record = (
        connection.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    receipt_record = (
        connection.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    output = connection.execute(
        sa.select(
            agent_receipts_v2.c.output_bytes, agent_receipts_v2.c.output_hash
        ).where(
            agent_receipts_v2.c.node_execution_id == request.node_execution_id.value
        )
    ).one_or_none()
    if intent_record is None or receipt_record is None or output is None:
        raise RunTransitionConflict(
            "successful effect-grant attempt has no exact confirmed effect receipt"
        )
    intent_snapshot = intent_snapshot_from_record(intent_record)
    receipt = receipt_from_record(receipt_record)
    if (
        intent_snapshot.state is not EffectIntentState.CONFIRMED
        or receipt.intent != intent_snapshot.intent
        or bytes(intent_snapshot.intent.request.payload) != bytes(output.output_bytes)
        or str(intent_snapshot.intent.request.request_hash.value)
        != str(output.output_hash)
    ):
        raise RunTransitionConflict(
            "successful effect-grant attempt has no exact confirmed effect receipt"
        )


def _agent_platform_effect_completion_is_deferred(
    connection: Any, node: AgentNodeV3
) -> bool:
    """Whether this node's kept output must wait for platform-effect settlement.

    An effect grant redeems only after the agent output has become durable, but
    the run may not leave the node until that redemption has a receipt. The
    grant document is the same pinned revision the node binding already read;
    a missing or invalid document is therefore durable corruption, not an
    absence that could make a completed run honest.
    """
    return agent_node_redeems_platform_effect(connection, node)


def _kept_transcript_values(
    connection: Any, transcript: AttemptTranscript | None
) -> dict[str, object]:
    """The attempt column this transcript sets, its bytes kept under that address.

    Keeping the material and naming it happen in the caller's own transaction,
    so no attempt can point at a transcript this store never got. The bytes
    arrive already bounded and redacted -- `AttemptTranscript` is the only way
    to make one -- so nothing here judges them a second time.
    """

    if transcript is None:
        return {}
    artifact = Artifact(transcript.document)
    keep_artifact(connection, artifact)
    return {
        agent_attempts.c.transcript_artifact_hash.name: artifact.artifact_hash.value
    }


def _fail_current_attempt(
    connection: Any,
    execution: AgentAttemptExecution,
    durable: AgentAttempt,
    failure: AgentAttemptFailureCode,
    receipt_reason: str,
    schema_revision: PublishedRevisionHash | None = None,
    judged_value: bytes | None = None,
    transcript: AttemptTranscript | None = None,
    redemption: ToolRedemptionReceipt | None = None,
    terminal_node_failure: bool = True,
) -> AgentAttemptFailed:
    """One durable failure seam for every way an armed attempt ends badly.

    The attempt turns `FAILED` under its named code and the `AGENT_FAILED` event
    carries that code. A terminal failure ends the run on the same node; the
    first output-schema refusal instead keeps the run `STARTED` while the exact
    repair is enqueued in this transaction. `receipt_reason` is the words of
    whoever judged this ending -- a compact schema-refusal diagnosis where an
    answer was refused, the supervision where a process died -- and every way
    through here carries one, because a failure whose reason is nowhere is the
    silent death this seam exists to end. A schema judgment also keeps the
    identity it judged; a process that died judged nothing, so those fields stay
    honestly empty.

    `judged_value` is the exact bytes that judgment read, and it arrives as
    bytes rather than as a hash because the seam that records the verdict is the
    only place that can also keep the evidence: the receipt's value hash is
    derived here, and the same bytes are held as an artifact under exactly that
    address in the same transaction. Without them a refused episode says only
    that something was refused and never what was written -- the receipt names a
    hash nothing resolves, and the one thing an operator needs to read is gone
    for good. Empty bytes are the exception: there is nothing to keep, and the
    hash of nothing already says so.

    `transcript` is what the executor decoded of the provider's own stream on
    the way to this ending, and it is kept here for the same reason: the ending
    an operator most needs to read is the one nobody can explain, and a failure
    whose steps are nowhere is that silence again one level down (#733).

    `redemption` is present only where the attempt's granted check *passed* and
    the attempt failed afterwards -- today, where the work could not be kept. It
    is written in this same transaction because it is evidence of something that
    really happened: dropping it because the ending turned out badly would erase
    a command that ran and exited zero, and leave an operator reading a failure
    with no way to tell whether the project's own check had ever been satisfied.
    Endings that failed *because* the check failed pass none, and there is
    nothing to write (#642).
    """
    request = execution.request
    attempt_id = execution.attempt_id
    value_hash = None if judged_value is None else Sha256Hash.of(judged_value)
    if judged_value:
        keep_artifact(connection, Artifact(judged_value))
    _keep_tool_redemption(connection, execution, redemption)
    if terminal_node_failure:
        keep_node_receipt(
            connection,
            request.node_execution_id,
            PersistedReceiptDisposition.FAILED,
            receipt_reason,
            schema_revision=schema_revision,
            value_hash=value_hash,
        )
    values: dict[str, object] = {
        "state": AgentAttemptState.FAILED.value,
        "state_version": durable.state_version + 1,
        "failure_code": failure.value,
        **_kept_transcript_values(connection, transcript),
    }
    updated = connection.execute(
        agent_attempts.update()
        .where(
            agent_attempts.c.attempt_id == attempt_id.value,
            agent_attempts.c.state == durable.state.value,
            agent_attempts.c.state_version == durable.state_version,
        )
        .values(**values)
    )
    if updated.rowcount != 1:
        raise RunTransitionConflict("agent failure lost its attempt CAS")
    record_attempt_ended(connection, attempt_id.value)
    durable_failure = _load_attempt(connection, attempt_id)
    _commit_event(
        connection,
        request.run_id,
        request.workflow_revision_hash,
        RunEventKind.AGENT_FAILED,
        failure.value.encode("ascii"),
        RunPosition(RunState.STARTED, request.node_id, request.round_ordinal),
        RunPosition(
            RunState.FAILED if terminal_node_failure else RunState.STARTED,
            request.node_id,
            request.round_ordinal,
        ),
        attempt_binding=RunEventAgentAttemptBinding(attempt_id, execution.ordinal),
    )
    return AgentAttemptFailed(durable_failure)


def _agent_declared_refusal(
    connection: Any,
    execution: AgentAttemptExecution,
    durable: AgentAttempt,
    declared: NodeOutput,
    result: AgentExecutionResult,
    redemption: ToolRedemptionReceipt | None,
) -> AgentAttemptFailed | None:
    """This node's declared refusal form, where the provider answered with one."""

    if declared.refusal is None:
        return None
    named = agent_refusal_reason(result.output_bytes)
    if named is None:
        return None
    return _fail_current_attempt(
        connection,
        execution,
        durable,
        AgentAttemptFailureCode.AGENT_REFUSED,
        node_receipt_reason(NodeReceiptReason.AGENT_REFUSED, named),
        AGENT_REFUSAL_SCHEMA.revision_hash,
        result.output_bytes,
        result.transcript,
        redemption,
    )


def _refused_by_the_project(
    connection: Any,
    execution: AgentAttemptExecution,
    durable: AgentAttempt,
    declared: NodeOutput,
    result: AgentExecutionResult,
    redemption: ToolRedemptionReceipt,
    evidence: ProjectVerificationFailureEvidence | None,
) -> AgentAttemptFailed:
    """End an attempt whose granted check said no, with the answer it said no to.

    The schema admitted these bytes, and that judgment is kept with the ending:
    a reader who cannot see what the provider answered cannot tell a broken
    build from a builder that did nothing (#1156).
    """

    return _fail_current_attempt(
        connection,
        execution,
        durable,
        AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED,
        node_receipt_reason(
            NodeReceiptReason.PROJECT_VERIFICATION_FAILED,
            verification_failure_verdict(redemption, evidence),
        ),
        PublishedRevisionHash(declared.schema_reference.revision),
        result.output_bytes,
        transcript=result.transcript,
    )


def _refused_produced_value(
    connection: Any,
    execution: AgentAttemptExecution,
    durable: AgentAttempt,
    declared: NodeOutput,
    produced: NoProducibleValue,
    transcript: AttemptTranscript | None,
    redemption: ToolRedemptionReceipt | None,
) -> AgentAttemptFailed:
    """End an attempt on the value the atelier composed, named after its author.

    No repair round: the one this store orders asks the provider to answer
    again, and the provider did not write what was refused here.
    """

    return _fail_current_attempt(
        connection,
        execution,
        durable,
        AgentAttemptFailureCode.PRODUCED_VALUE_REFUSED,
        node_receipt_reason(NodeReceiptReason.PRODUCED_VALUE_REFUSED, produced.verdict),
        PublishedRevisionHash(declared.schema_reference.revision),
        produced.judged,
        transcript=transcript,
        redemption=redemption,
    )


def _store_output_schema_refusal_receipt(
    connection: Any,
    attempt_id: AgentAttemptId,
    reason: str,
    schema_revision: PublishedRevisionHash,
    value: bytes,
) -> OutputSchemaRefusalReceipt:
    artifact = None if value == b"" else Artifact(value)
    if artifact is not None:
        keep_artifact(connection, artifact)
    receipt = OutputSchemaRefusalReceipt(
        attempt_id,
        reason,
        schema_revision,
        Sha256Hash.of(value),
        None if artifact is None else artifact.artifact_hash,
    )
    connection.execute(
        agent_attempt_receipts_v3.insert()
        .prefix_with(_OR_IGNORE)
        .values(
            attempt_id=receipt.attempt_id.value,
            reason=receipt.reason,
            schema_revision_hash=receipt.schema_revision.value,
            value_hash=receipt.value_hash.value,
            artifact_hash=(
                None if receipt.artifact_hash is None else receipt.artifact_hash.value
            ),
            receipt_hash=receipt.receipt_hash.value,
        )
    )
    attempt = _load_attempt(connection, attempt_id)
    durable = load_output_schema_refusal_receipt(
        connection,
        attempt_id,
        expected_node_execution_id=attempt.node_execution_id,
        expected_attempt_ordinal=attempt.attempt_ordinal,
        expected_schema_revision=schema_revision,
    )
    if durable is None or durable != receipt:
        raise RunTransitionConflict("durable output-schema refusal receipt differs")
    return durable


def _kept_verdict(
    session: Any,
    graph: WorkflowGraphV3,
    request: AgentExecutionRequestV2,
) -> Verdict | None:
    """The verdict a finished execution of this node steers its loop with.

    Asked only where the document says a verdict decides something, so a run
    that steers nothing reads nothing. Where it does decide, the answer comes
    back from what that round kept rather than from anything recomputed: this is
    the recovery path, and the continuation it reports has to be the one the
    success write already took.
    """
    if verdict_condition_of(graph, request.node_id) is None:
        return None
    return read_verdict(load_kept_value(session, request.node_execution_id))


def _proof_of_a_passed_check(
    redemption: ToolRedemptionReceipt | None,
) -> ToolRedemptionReceipt | None:
    """This redemption where the project's check passed, and nothing where not.

    Every ending that keeps proof beside a failure asks here rather than reading
    the receipt itself, so no branch can persist a row saying the check did not
    pass. A nonzero exit is not a weaker proof of the same thing: it is the
    verdict that ends the attempt under `PROJECT_VERIFICATION_FAILED`, and a
    stored redemption is by definition the record of a command that was
    satisfied.
    """

    if redemption is None or not redemption.satisfied_the_project:
        return None
    return redemption


def _keep_tool_redemption(
    connection: Any,
    execution: AgentAttemptExecution,
    redemption: ToolRedemptionReceipt | None,
) -> None:
    """Keep what this attempt's grant redeemed, inside the write that succeeds it.

    A retry of the same durable attempt runs the verification again, and the row
    that is already there decides: identical evidence is the same redemption
    written twice, and different evidence is two answers about one attempt,
    which is a contradiction rather than a second receipt.

    Read back by the attempt, because the attempt is what this row is keyed by:
    a node execution can have a second attempt with a redemption of its own, and
    asking by node execution would find that one and call it a contradiction.
    """
    if redemption is None:
        return
    if not redemption.satisfied_the_project:
        raise ToolRedemptionConflict(
            "a stored tool redemption is the record of a check that passed"
        )
    if (
        redemption.node_execution_id != execution.request.node_execution_id
        or redemption.attempt_id != execution.attempt_id
    ):
        raise ToolRedemptionConflict("tool redemption differs from its exact attempt")
    connection.execute(
        tool_redemptions.insert()
        .prefix_with(_OR_IGNORE)
        .values(_tool_redemption_values(redemption))
    )
    stored = (
        connection.execute(
            sa.select(tool_redemptions).where(
                tool_redemptions.c.attempt_id == redemption.attempt_id.value
            )
        )
        .mappings()
        .one()
    )
    if _tool_redemption_from_record(stored) != redemption:
        raise ToolRedemptionConflict(
            "durable tool redemption differs from exact redemption"
        )


def _permission_receipt_values(receipt: PermissionReceipt) -> dict[str, object]:
    """One answered question as its row, in the ledger's own spelling."""

    return {
        "attempt_id": receipt.attempt_id.value,
        "correlation_id": receipt.correlation_id.value,
        "effect": receipt.effect.value,
        "scope_kind": receipt.scope.kind.value,
        "scope_value": receipt.scope.value,
        "granted": receipt.granted,
        "policy_revision_hash": receipt.policy_revision_hash.value,
        "authority": receipt.authority.value,
        "decided_at": receipt.decided_at.value,
        "receipt_hash": receipt.receipt_hash.value,
    }


def _permission_receipt_from_record(record: Mapping[Any, Any]) -> PermissionReceipt:
    """The receipt this row holds, re-derived rather than trusted."""

    try:
        return PermissionReceipt(
            AgentAttemptId(str(record["attempt_id"])),
            PermissionCorrelationId(str(record["correlation_id"])),
            PermissionEffect(str(record["effect"])),
            PermissionScope(
                PermissionScopeKind(str(record["scope_kind"])),
                str(record["scope_value"]),
            ),
            bool(record["granted"]),
            PermissionPolicyRevisionHash(str(record["policy_revision_hash"])),
            PermissionAuthority(str(record["authority"])),
            RecordedAt(str(record["decided_at"])),
        )
    except ValueError as error:
        raise PermissionReceiptConflict(
            "durable permission receipt is not one this product wrote"
        ) from error


_CANCELLATION_END_EVENT_BY_STATE = {
    AgentAttemptState.CANCELLED: RunEventKind.AGENT_CANCELLED,
    AgentAttemptState.INTERRUPTED: RunEventKind.AGENT_INTERRUPTED,
}


def _insert_attempt_event(
    connection: Any,
    attempt: AgentAttempt,
    kind: RunEventKind,
    *,
    command: AgentAttemptCancellation | None = None,
    replacement_attempt_id: AgentAttemptId | None = None,
) -> None:
    run = load_run(connection, attempt.run_id)
    sequence = run.last_event_sequence + 1
    payload = b"" if command is None else command.command_id.encode("utf-8")
    attempt_binding = (
        RunEventAgentAttemptBinding(attempt.attempt_id, attempt.attempt_ordinal)
        if command is None
        else RunEventCancellationBinding(
            attempt.attempt_id,
            attempt.attempt_ordinal,
            command.replacement,
            command.command_id,
            command.disposition,
            replacement_attempt_id,
        )
    )
    event = RunEvent(
        attempt.run_id,
        attempt.workflow_revision_hash,
        sequence,
        attempt.node_id,
        attempt.node_execution_id,
        kind,
        payload,
        attempt_binding=attempt_binding,
    )
    updated = connection.execute(
        runs.update()
        .where(
            runs.c.run_id == attempt.run_id.value,
            runs.c.revision_hash == attempt.workflow_revision_hash.value,
            runs.c.current_node_id == attempt.node_id,
            runs.c.state == RunState.STARTED.value,
            runs.c.state_version == run.state_version,
            runs.c.last_event_sequence == run.last_event_sequence,
        )
        .values(
            state_version=run.state_version + 1,
            last_event_sequence=sequence,
        )
    )
    if updated.rowcount != 1:
        raise RunTransitionConflict("agent attempt event lost the run-head CAS")
    _insert_event(connection, event)


def _lift_run_under_operator_cancel(connection: Any, terminal: AgentAttempt) -> None:
    """Close the run under its own operator cancel, in the same TX as its receipt.

    `CANCELLED` and `INTERRUPTED` are the two words a cleanup attestation can
    leave an attempt in, and #439 Bauplan P3 lifts the run the same way under
    both: the run's own ending follows the *command's* identity, not which of
    the two the disposition happened to be (the two-axis doctrine -- the node
    says how its own work ended, the run says why it stood still). A
    driver-loss or unavailable-executor cleanup reaches these same attempt
    words through a command of its own, never the operator's --
    `is_operator_run_cancel` is what tells the two apart, so neither of those
    two other callers of this cleanup path is touched here, and their runs
    stay exactly as unlifted and receipt-less as before this method existed.

    A replacement is never in flight here: `request_run_cancellation` always
    submits `AgentAttemptReplacement.NONE` (D2, #439 Bauplan P2/P3), so an
    operator command reaching this point never shares its node execution with
    a second attempt.
    """
    cancellation = terminal.cancellation
    if (
        cancellation is None
        or not is_operator_run_cancel(cancellation.command_id)
        or cancellation.replacement is not AgentAttemptReplacement.NONE
        or terminal.state
        not in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}
    ):
        return
    run = load_run(connection, terminal.run_id)
    if run.state is not RunState.STARTED:
        return
    live_node_execution_id = NodeExecutionId.for_node(
        run.run_id, run.revision_hash, run.current_node_id, run.current_round_ordinal
    )
    if live_node_execution_id != terminal.node_execution_id:
        return
    if cancellation.disposition is None:
        raise RunTransitionConflict(
            "a terminal cancellation lifts its run only with an attested disposition"
        )
    keep_node_receipt(
        connection,
        terminal.node_execution_id,
        PersistedReceiptDisposition.CANCELLED,
        node_receipt_reason(
            NodeReceiptReason.CANCELLED_BY_OPERATOR, cancellation.disposition.value
        ),
    )
    lift_started_run(
        connection,
        run.run_id,
        run.revision_hash,
        run.state_version,
        run.last_event_sequence,
        RunState.CANCELLED,
    )


def _run_cancellation_from_event_log(
    connection: Any, run_id: RunId, command_id: str
) -> RunCancellationResult | None:
    """One accepted run-cancel command's canonical answer once its attempt
    row has moved on.

    Only a runner-carried success ever clears an attempt's cancellation
    columns (`_commit_success`), and it always does so in the very
    transaction that also writes that attempt's `AGENT_COMPLETED` event -- so
    by the time this command's row is gone, the event that decided its fate
    is already durable too. `None` means this exact command was never
    accepted at all: the caller is free to treat it as genuinely new.
    """
    requested = (
        connection.execute(
            sa.select(run_events.c.agent_attempt_id, run_events.c.event_sequence).where(
                run_events.c.run_id == run_id.value,
                run_events.c.cancellation_command_id == command_id,
                run_events.c.event_kind == RunEventKind.AGENT_CANCEL_REQUESTED.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    if requested is None:
        return None
    terminal_kind = connection.scalar(
        sa.select(run_events.c.event_kind)
        .where(
            run_events.c.run_id == run_id.value,
            run_events.c.agent_attempt_id == requested["agent_attempt_id"],
            run_events.c.event_sequence > requested["event_sequence"],
            run_events.c.event_kind.in_(
                (
                    RunEventKind.AGENT_CANCELLED.value,
                    RunEventKind.AGENT_INTERRUPTED.value,
                    RunEventKind.AGENT_COMPLETED.value,
                )
            ),
        )
        .order_by(run_events.c.event_sequence)
        .limit(1)
    )
    run = load_run(connection, run_id)
    if terminal_kind == RunEventKind.AGENT_COMPLETED.value:
        return RunCancellationOvertakenBySuccess(run)
    if terminal_kind in (
        RunEventKind.AGENT_CANCELLED.value,
        RunEventKind.AGENT_INTERRUPTED.value,
    ):
        return RunCancellationTerminalRetry(run)
    return PortDurableStateCorrupt()


def _wait_cancellation_from_event_log(
    connection: Any, run_id: RunId, command_id: str
) -> RunCancellationResult | None:
    """This command's answer when it already ended a run resting at a pause.

    A resting Wait holds no attempt row, so the command id lives in the one
    place a wait cancellation writes it: the payload of its own attestation.
    That event is never rewritten, so a retry after a lost response reads the
    same answer forever. `None` means no wait cancellation carries this command.
    """
    ended = connection.scalar(
        sa.select(run_events.c.event_sequence).where(
            run_events.c.run_id == run_id.value,
            run_events.c.event_kind == RunEventKind.WAIT_CANCELLED.value,
            run_events.c.payload == command_id.encode("utf-8"),
        )
    )
    if ended is None:
        return None
    return RunCancellationEndedRun(load_run(connection, run_id))


def _known_run_cancellation(
    connection: Connection, run_id: RunId, attempt: AgentAttempt
) -> RunCancellationResult:
    """The answer a command already stamped on its attempt, whatever it reached."""
    if attempt.state is AgentAttemptState.CANCEL_REQUESTED:
        return RunCancellationAccepted(attempt)
    if attempt.state in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}:
        return RunCancellationTerminalRetry(load_run(connection, run_id))
    return PortDurableStateCorrupt()


def _resting_wait_run(run: AnyRun) -> RunV3 | None:
    """The run when it rests at a pause a format-3 line wrote, else nothing."""
    if run.state is RunState.WAITING_INPUT and isinstance(run, RunV3):
        return run
    return None


def _cancel_resting_wait(
    connection: Any,
    run: RunV3,
    node_execution_id: NodeExecutionId,
    command_id: str,
) -> RunCancellationResult:
    """End a run resting at a pause, in the transaction that resolved it.

    Nothing is enqueued and nothing converges later: a pause has no attempt to
    stop, so the command writes its own attestation and the run is over when
    this returns. That is why the answer is `EndedRun` rather than `Accepted` --
    there is no cleanup an operator could still be waiting on.

    A pending answer is the one thing that refuses. The product has already told
    a person their message was taken, and applying it is a separate transaction
    away, so ending the run here would drop it silently; the operator is told to
    retry once that message has landed.
    """
    pending_answer = connection.scalar(
        sa.select(wait_answers.c.node_execution_id).where(
            wait_answers.c.node_execution_id == node_execution_id.value,
            wait_answers.c.state == WaitAnswerState.PENDING.value,
        )
    )
    if pending_answer is not None:
        return RunCancellationNotCancellable(RunCancellationRefusal.ANSWER_IN_FLIGHT)
    commit_wait_cancelled(
        connection,
        run.run_id,
        run.revision_hash,
        run.current_node_id,
        command_id,
        run.current_round_ordinal,
    )
    return RunCancellationEndedRun(load_run(connection, run.run_id))


def _unavailable_executor_cleanup_command_id(attempt_id: AgentAttemptId) -> str:
    return (
        f"{AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value}:{attempt_id.value}"
    )


def _unavailable_executor_cleanup_request(
    attempt: AgentAttempt,
) -> CancelAgentAttemptRequest:
    cancellation = attempt.cancellation
    if cancellation is not None:
        return CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            cancellation.command_id,
            cancellation.expected_attempt_state_version,
            cancellation.replacement,
        )
    return CancelAgentAttemptRequest(
        attempt.run_id,
        attempt.attempt_id,
        _unavailable_executor_cleanup_command_id(attempt.attempt_id),
        attempt.state_version,
        AgentAttemptReplacement.NONE,
    )


def _is_unavailable_executor_cleanup(attempt: AgentAttempt) -> bool:
    cancellation = attempt.cancellation
    return (
        attempt.attempt_ordinal == 1
        and attempt.runner_manifest_id is None
        and cancellation is not None
        and cancellation.command_id
        == _unavailable_executor_cleanup_command_id(attempt.attempt_id)
        and cancellation.replacement is AgentAttemptReplacement.NONE
    )


def _is_unavailable_executor_cleanup_complete(attempt: AgentAttempt) -> bool:
    cancellation = attempt.cancellation
    return (
        _is_unavailable_executor_cleanup(attempt)
        and attempt.state is AgentAttemptState.CANCELLED
        and attempt.process_phase is AgentAttemptProcessPhase.CLEANUP_ATTESTED
        and cancellation is not None
        and cancellation.disposition
        is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
    )


def _commit_unavailable_executor_refusal(
    connection: Any, request: AgentExecutionRequestV2
) -> None:
    _commit_event(
        connection,
        request.run_id,
        request.workflow_revision_hash,
        RunEventKind.AGENT_FAILED,
        AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii"),
        RunPosition(RunState.STARTED, request.node_id, request.round_ordinal),
        RunPosition(RunState.FAILED, request.node_id, request.round_ordinal),
    )


def _unavailable_executor_refusal_is_already_terminal(
    connection: Any,
    request: AgentExecutionRequestV2,
    run: RunV2 | RunV3,
) -> bool:
    """Whether this exact pre-attempt terminal transition already committed."""

    if (
        run.state is not RunState.FAILED
        or run.current_node_id != request.node_id
        or run.current_round_ordinal != request.round_ordinal
    ):
        return False
    return (
        connection.execute(
            sa.select(run_events.c.event_sequence).where(
                run_events.c.run_id == request.run_id.value,
                run_events.c.revision_hash == request.workflow_revision_hash.value,
                run_events.c.node_id == request.node_id,
                run_events.c.node_execution_id == request.node_execution_id.value,
                run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
                run_events.c.payload
                == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode(
                    "ascii"
                ),
                run_events.c.agent_attempt_id.is_(None),
                run_events.c.attempt_ordinal.is_(None),
                run_events.c.round_ordinal == request.round_ordinal,
            )
        ).one_or_none()
        is not None
    )


class DbosAgentAttemptStore:
    def __init__(self, engine: Engine, application_version: str | None = None) -> None:
        self._engine = engine
        self._application_version = application_version

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        request = execution.request
        prepared = _prepared_attempt(execution)
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
                or execution.ordinal != 1
            ):
                existing = _load_attempt(connection, prepared.attempt_id)
                _require_attempt_binding(existing, execution)
                return existing
            inserted = connection.execute(
                agent_attempts.insert()
                .prefix_with(_OR_IGNORE)
                .values(_attempt_values(prepared))
            )
            if inserted.rowcount == 1:
                record_attempt_started(connection, prepared.attempt_id.value)
            durable = _load_attempt(connection, prepared.attempt_id)
            _require_attempt_binding(durable, execution)
            return durable

    def refuse_unavailable_executor(
        self, request: AgentExecutionRequestV2
    ) -> AgentExecutorBindingRefusalResult:
        """Close an unclaimed Agent node without inventing an attempt failure.

        The only mutable predecessor is ordinal one in PREPARED, which has not
        crossed the launch boundary. It first returns its existing cancellation
        cleanup request; callers carry that through the normal supervisor and
        workspace path, then retry this method. The same command, once accepted,
        stays on that cleanup path until NEVER_LAUNCHED is attested. Every armed,
        runner-bound, or foreign cancellation-in-progress record is fenced for
        #15.
        """

        attempt_id = AgentAttemptId.for_execution(
            request.node_execution_id, request.request_hash, 1
        )
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(connection, request, attempt_id, 1)
            if _unavailable_executor_refusal_is_already_terminal(
                connection, request, run
            ):
                return AgentExecutorBindingRefusalWritten()
            record = (
                connection.execute(
                    sa.select(agent_attempts).where(
                        agent_attempts.c.attempt_id == attempt_id.value
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                _commit_unavailable_executor_refusal(connection, request)
                return AgentExecutorBindingRefusalWritten()
            attempt = attempt_from_record(record)
            if (
                attempt.node_execution_id != request.node_execution_id
                or attempt.request_hash != request.request_hash
                or attempt.run_id != request.run_id
                or attempt.workflow_revision_hash != request.workflow_revision_hash
                or attempt.node_id != request.node_id
                or attempt.attempt_ordinal != 1
            ):
                raise RunTransitionConflict(
                    "unavailable executor differs from durable attempt binding"
                )
            if (
                attempt.state is AgentAttemptState.PREPARED
                and attempt.runner_manifest_id is None
            ) or (
                _is_unavailable_executor_cleanup(attempt)
                and not _is_unavailable_executor_cleanup_complete(attempt)
            ):
                return AgentExecutorBindingRefusalNeedsPreparedCleanup(
                    attempt, _unavailable_executor_cleanup_request(attempt)
                )
            if _is_unavailable_executor_cleanup_complete(attempt):
                _commit_unavailable_executor_refusal(connection, request)
                return AgentExecutorBindingRefusalWritten()
            return AgentExecutorBindingRefusalFenced(attempt)

    def bind_watchdog(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(
                connection,
                execution.request,
                execution.attempt_id,
                execution.ordinal,
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.process_phase is AgentAttemptProcessPhase.WATCHDOG_READY:
                if (
                    durable.process_owner_id != process_owner_id
                    or durable.watchdog_generation_id != watchdog_generation_id
                ):
                    raise RunTransitionConflict(
                        "watchdog retry differs from durable generation"
                    )
                return durable
            if (
                durable.state is not AgentAttemptState.PREPARED
                or durable.process_phase is not AgentAttemptProcessPhase.NONE
                or durable.runner_manifest_id is not None
            ):
                raise RunTransitionConflict(
                    "only a legacy unbound prepared attempt can bind a watchdog"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.process_phase
                    == AgentAttemptProcessPhase.NONE.value,
                )
                .values(
                    state_version=durable.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.WATCHDOG_READY.value,
                    process_owner_id=process_owner_id.value,
                    watchdog_generation_id=watchdog_generation_id.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("watchdog binding lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult:
        request = execution.request
        attempt_id = execution.attempt_id
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.runner_manifest_id is not None:
                raise RunTransitionConflict(
                    "a runner-bound attempt cannot enter the legacy claim path"
                )
            if durable.state is AgentAttemptState.PREPARED:
                if (
                    run.state is not RunState.STARTED
                    or run.current_node_id != request.node_id
                    or run.current_round_ordinal != request.round_ordinal
                ):
                    raise RunTransitionConflict(
                        "prepared attempt no longer owns current node"
                    )
                updated = connection.execute(
                    agent_attempts.update()
                    .where(
                        agent_attempts.c.attempt_id == attempt_id.value,
                        agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                        agent_attempts.c.state_version == durable.state_version,
                    )
                    .values(
                        state=AgentAttemptState.LAUNCH_ARMED.value,
                        state_version=durable.state_version + 1,
                        process_phase=(
                            AgentAttemptProcessPhase.LAUNCH_AUTHORIZED.value
                            if durable.process_phase
                            is AgentAttemptProcessPhase.WATCHDOG_READY
                            else AgentAttemptProcessPhase.NONE.value
                        ),
                    )
                )
                durable = _load_attempt(connection, attempt_id)
                if updated.rowcount == 1:
                    return AgentAttemptClaimedByThisCall(durable)
            if durable.state is AgentAttemptState.LAUNCH_ARMED:
                return AgentAttemptPossiblyRan(durable)
            if durable.state is AgentAttemptState.FAILED:
                return AgentAttemptFailed(durable)
            if durable.state in {
                AgentAttemptState.CANCEL_REQUESTED,
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                return AgentAttemptPossiblyRan(durable)
            if durable.state is AgentAttemptState.SUCCEEDED:
                completion = completion_after_node(
                    graph,
                    request.node_id,
                    request.round_ordinal,
                    _kept_verdict(connection, graph, request),
                )
                _require_completed_attempt_head(
                    connection,
                    run,
                    request,
                    completion,
                    _agent_platform_effect_completion_is_deferred(
                        connection, _agent_node_for_attempt(graph, request.node_id)
                    ),
                )
                return AgentAttemptSucceeded(durable, completion)
            raise AssertionError("closed agent attempt state was not exhaustive")

    def record_permission_decision(self, receipt: PermissionReceipt) -> None:
        """Insert, then read back what stands, and compare the two identities.

        The identity rather than the whole row, because the row also carries
        when it was written and the receipt does not: a recovered attempt asks
        its question again seconds or days later, and that second write must
        find the answer it already gave rather than a disagreement about which
        second the clock read.

        The read-back also refuses a row whose stored hash column does not
        match the hash its own content re-derives: a schema-valid row is not
        proof the column beside it was never altered outside this write. That
        disagreement raises `DurableStateCorrupt` rather than
        `PermissionReceiptConflict` -- it is not a second answer to reconcile,
        it is a row this store no longer trusts.
        """

        with canonical_write_transaction(self._engine) as connection:
            connection.execute(
                permission_receipts.insert()
                .prefix_with(_OR_IGNORE)
                .values(_permission_receipt_values(receipt))
            )
            stored = (
                connection.execute(
                    sa.select(permission_receipts).where(
                        permission_receipts.c.attempt_id == receipt.attempt_id.value,
                        permission_receipts.c.correlation_id
                        == receipt.correlation_id.value,
                    )
                )
                .mappings()
                .one()
            )
            reconstructed_hash = _permission_receipt_from_record(stored).receipt_hash
            if reconstructed_hash.value != str(stored["receipt_hash"]):
                raise DurableStateCorrupt(
                    "permission receipt does not hash to its stored column "
                    f"(attempt {receipt.attempt_id.value}, "
                    f"correlation {receipt.correlation_id.value})"
                )
            if reconstructed_hash != receipt.receipt_hash:
                raise PermissionReceiptConflict(
                    "durable permission receipt differs from the decision offered"
                )

    def observe_process(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(
                connection,
                execution.request,
                execution.attempt_id,
                execution.ordinal,
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.process_phase is AgentAttemptProcessPhase.PROCESS_OBSERVED:
                if (
                    durable.process_owner_id != process_owner_id
                    or durable.watchdog_generation_id != watchdog_generation_id
                ):
                    raise RunTransitionConflict(
                        "observed process retry differs from durable generation"
                    )
                return durable
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.LAUNCH_ARMED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.process_phase
                    == AgentAttemptProcessPhase.LAUNCH_AUTHORIZED.value,
                    agent_attempts.c.process_owner_id == process_owner_id.value,
                    agent_attempts.c.watchdog_generation_id
                    == watchdog_generation_id.value,
                )
                .values(
                    state_version=durable.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.PROCESS_OBSERVED.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("process observation lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt:
        with self._engine.connect() as connection:
            return _load_attempt(connection, attempt_id)

    def iter_driverless_attempts(self, page_limit: PageLimit) -> Iterator[AgentAttempt]:
        """Ask only after the durable runtime is launched.

        Before the launch, the workflow table this reads is either absent or
        still holds the statuses of the process that died, so every answer it
        could give would be about a machine that is not running yet. Which is
        also why the runtime's own application version is required rather than
        optional: a `PENDING` row a retired version left behind is never going to
        be recovered, and counting it as a live driver would hide this attempt
        from the sweep forever.
        """

        if self._application_version is None:
            raise RunTransitionConflict(
                "the driverless sweep requires the runtime application version"
            )
        terminal_states = tuple(state.value for state in TERMINAL_AGENT_ATTEMPT_STATES)
        after: AgentAttemptId | None = None
        while True:
            with self._engine.connect() as connection:
                query = sa.select(agent_attempts).where(
                    agent_attempts.c.state.not_in(terminal_states),
                    agent_attempts.c.runner_manifest_id.is_(None),
                )
                if after is not None:
                    query = query.where(agent_attempts.c.attempt_id > after.value)
                candidates = tuple(
                    attempt_from_record(record)
                    for record in connection.execute(
                        query.order_by(agent_attempts.c.attempt_id).limit(
                            page_limit.value
                        )
                    ).mappings()
                )
                if not candidates:
                    return
                drivers = tuple(
                    (attempt, driving_workflow_ids(attempt)) for attempt in candidates
                )
                driving = live_driver_workflow_ids(
                    connection,
                    (
                        workflow_id
                        for _attempt, workflow_ids in drivers
                        for workflow_id in workflow_ids
                    ),
                    self._application_version,
                )
            after = candidates[-1].attempt_id
            for attempt, workflow_ids in drivers:
                if driving.isdisjoint(workflow_ids):
                    yield attempt
            if len(candidates) < page_limit.value:
                return

    def _commit_success(
        self,
        connection: Any,
        execution: AgentAttemptExecution,
        durable: AgentAttempt,
        run: RunV2 | RunV3,
        graph: WorkflowGraphV3,
        result: AgentExecutionResult,
        *,
        redemption: ToolRedemptionReceipt | None = None,
        verification_failure_evidence: ProjectVerificationFailureEvidence | None = None,
        candidate_diff: str | None = None,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        request = execution.request
        node = _agent_node_for_attempt(graph, request.node_id)
        declared = node.outputs[0]
        declared_a_refusal = _agent_declared_refusal(
            connection,
            execution,
            durable,
            declared,
            result,
            _proof_of_a_passed_check(redemption),
        )
        if declared_a_refusal is not None:
            return declared_a_refusal
        schema_document = declared_output_schema_document(connection, node.id, declared)
        refusal = declared_output_schema_refusal(
            schema_document, node.id, declared, result.output_bytes
        )
        if refusal is not None:
            receipt_reason = schema_refusal_receipt_reason(
                refusal, result.output_bytes, NodeReceiptReason.OUTPUT_SCHEMA_REFUSED
            )
            refusal_receipt = _store_output_schema_refusal_receipt(
                connection,
                durable.attempt_id,
                receipt_reason,
                PublishedRevisionHash(declared.schema_reference.revision),
                result.output_bytes,
            )
            if execution.ordinal == 1:
                return self._begin_output_schema_repair(
                    connection,
                    execution,
                    durable,
                    graph,
                    node,
                    result,
                    refusal_receipt,
                    redemption,
                )
            failed = _fail_current_attempt(
                connection,
                execution,
                durable,
                AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED,
                receipt_reason,
                PublishedRevisionHash(declared.schema_reference.revision),
                result.output_bytes,
                result.transcript,
                _proof_of_a_passed_check(redemption),
            )
            return failed
        if redemption is not None and redemption.exit_code != 0:
            return _refused_by_the_project(
                connection,
                execution,
                durable,
                declared,
                result,
                redemption,
                verification_failure_evidence,
            )
        node_value = the_value_this_execution_produced(
            schema_document, node.id, declared, result.output_bytes, candidate_diff
        )
        if isinstance(node_value, NoProducibleValue):
            return _refused_produced_value(
                connection,
                execution,
                durable,
                declared,
                node_value,
                result.transcript,
                _proof_of_a_passed_check(redemption),
            )
        receipt = AgentReceiptV2.for_execution(request, run.binding_set_hash, result)
        connection.execute(
            agent_receipts_v2.insert()
            .prefix_with(_OR_IGNORE)
            .values(_agent_receipt_v2_values(receipt))
        )
        receipt_record = (
            connection.execute(
                sa.select(agent_receipts_v2).where(
                    agent_receipts_v2.c.node_execution_id
                    == request.node_execution_id.value
                )
            )
            .mappings()
            .one()
        )
        if _agent_receipt_v2_from_record(receipt_record) != receipt:
            raise AgentReceiptConflict(
                "durable V2 agent receipt differs from exact result"
            )
        _keep_tool_redemption(connection, execution, redemption)
        keep_node_receipt(
            connection,
            request.node_execution_id,
            PersistedReceiptDisposition.SUCCEEDED,
            node_receipt_reason(NodeReceiptReason.OUTPUT_ACCEPTED),
            NodeArtifact(
                request.run_id,
                node.id,
                request.node_execution_id,
                declared.name,
                PublishedRevisionHash(declared.schema_reference.revision),
                node_value,
            ),
        )
        values: dict[str, object] = {
            "state": AgentAttemptState.SUCCEEDED.value,
            "state_version": durable.state_version + 1,
            "receipt_hash": receipt.receipt_hash.value,
            **_kept_transcript_values(connection, result.transcript),
        }
        updated = connection.execute(
            agent_attempts.update()
            .where(
                agent_attempts.c.attempt_id == durable.attempt_id.value,
                agent_attempts.c.state == durable.state.value,
                agent_attempts.c.state_version == durable.state_version,
            )
            .values(**values)
        )
        if updated.rowcount != 1:
            raise RunTransitionConflict("agent success lost its attempt CAS")
        record_attempt_ended(connection, durable.attempt_id.value)
        completion = completion_after_node(
            graph,
            request.node_id,
            request.round_ordinal,
            None
            if verdict_condition_of(graph, request.node_id) is None
            else read_verdict(result.output_bytes),
        )
        source = RunPosition(RunState.STARTED, request.node_id, request.round_ordinal)
        if _agent_platform_effect_completion_is_deferred(connection, node):
            target = source
        else:
            match completion:
                case RunContinues(node_id, target_round):
                    target = RunPosition(RunState.STARTED, node_id, target_round)
                case RunCompletes():
                    target = RunPosition(
                        RunState.COMPLETED, request.node_id, request.round_ordinal
                    )
                case _ as unreachable:
                    assert_never(unreachable)
        _commit_event(
            connection,
            request.run_id,
            request.workflow_revision_hash,
            RunEventKind.AGENT_COMPLETED,
            node_value,
            source,
            target,
            attempt_binding=RunEventAgentAttemptBinding(
                durable.attempt_id, execution.ordinal
            ),
            agent_receipt_hash=receipt.receipt_hash,
        )
        return AgentAttemptSucceeded(
            _load_attempt(connection, durable.attempt_id), completion
        )

    def _begin_output_schema_repair(
        self,
        connection: Any,
        execution: AgentAttemptExecution,
        durable: AgentAttempt,
        graph: WorkflowGraphV3,
        node: AgentNodeV3,
        result: AgentExecutionResult,
        refusal_receipt: OutputSchemaRefusalReceipt,
        redemption: ToolRedemptionReceipt | None,
    ) -> AgentAttemptFailed:
        request = execution.request
        failed = _fail_current_attempt(
            connection,
            execution,
            durable,
            AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED,
            refusal_receipt.reason,
            refusal_receipt.schema_revision,
            result.output_bytes,
            result.transcript,
            _proof_of_a_passed_check(redemption),
            terminal_node_failure=False,
        )
        repair_request = _output_schema_repair_request(
            connection, request, graph, node, refusal_receipt
        )
        repair = _prepared_attempt(
            AgentAttemptExecution(
                repair_request,
                AgentAttemptId.for_execution(
                    repair_request.node_execution_id,
                    repair_request.request_hash,
                    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
                ),
                REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
            )
        )
        connection.execute(agent_attempts.insert().values(_attempt_values(repair)))
        record_attempt_started(connection, repair.attempt_id.value)
        if self._application_version is None:
            raise RunTransitionConflict(
                "output-schema repair requires an application version"
            )
        self._enqueue_workflow(
            connection,
            REPLACEMENT_WORKFLOW_NAME,
            replacement_workflow_id_for(repair.attempt_id),
            self._application_version,
            repair.attempt_id.value,
        )
        return failed

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: ToolRedemptionReceipt | None = None,
        verification_failure_evidence: ProjectVerificationFailureEvidence | None = None,
        candidate_diff: str | None = None,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        """Write the one success this attempt is allowed, or its named refusal.

        A V3 node's declared output is what its author promised, so the exact
        decoded bytes are read against the schema it pins before anything here is
        written. Bytes their own schema refuses leave no agent receipt, no
        `AGENT_COMPLETED` event and no advanced run -- a success nobody may take
        back must not be written for an answer this product cannot honour. What
        the refusal leaves instead is its durable name: an immutable Attempt
        receipt carrying the compact schema-refusal diagnosis. An ordinal-one
        refusal records a nonterminal `AGENT_FAILED` event and orders its repair;
        only an ordinal-two refusal also writes the terminal `failed`
        `node-receipt/v3`. Both attempts use the same failure seam
        `PROCESS_EXITED_UNSUCCESSFULLY` runs on today.
        A granted verification that exits nonzero is the same named seam under
        `PROJECT_VERIFICATION_FAILED`, with how the command ended in the reason
        and without a `tool_redemptions` row. `verification_failure_evidence`
        names, in that same reason, pytest's own summary line where the retained
        tail carried one, and the address that tail was kept under.

        A V3 success additionally keeps what the run now knows durably: the
        produced value (`produced_node_values.py`) as `node-artifact/v3` and the
        `succeeded` `node-receipt/v3` naming it, in this same transaction.
        """
        request = execution.request
        attempt_id = execution.attempt_id
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
                or run.current_round_ordinal != request.round_ordinal
            ):
                raise RunTransitionConflict(
                    "only the armed current attempt can succeed"
                )
            return self._commit_success(
                connection,
                execution,
                durable,
                run,
                graph,
                result,
                redemption=redemption,
                verification_failure_evidence=verification_failure_evidence,
                candidate_diff=candidate_diff,
            )

    def complete_known_failure(
        self,
        execution: AgentAttemptExecution,
        exit_signature: ProcessExitSignature,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End the attempt whose process left no answer, and say what it left.

        The exit signature is what the supervision saw from outside: how the
        child ended and the standard error it left. It reaches the `failed`
        `node-receipt/v3` on the same seam a refused answer uses, and no further
        -- the `AGENT_FAILED` event keeps carrying the bare code, so the stream
        stays the bounded surface a reader may subscribe to without reading a
        provider's own output.

        `transcript` is what the executor could read of what the process itself
        wrote, and it is kept beside that. An exit code and an empty standard
        error was the whole account of a real failed run (#733), which is to say
        no account at all; the steps the process got through, and whatever it
        printed instead of a stream, are the only place the reason can be.
        """
        request = execution.request
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict("only the armed current attempt can fail")
            return _fail_current_attempt(
                connection,
                execution,
                durable,
                AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
                node_receipt_reason(
                    NodeReceiptReason.PROCESS_EXITED_UNSUCCESSFULLY,
                    process_exit_verdict(exit_signature, transcript),
                ),
                transcript=transcript,
            )

    def complete_agent_refusal(
        self, execution: AgentAttemptExecution, reason: str
    ) -> AgentAttemptFailed:
        """End an armed attempt whose executor did not start a provider process."""

        return self._judged_armed_failure(
            execution,
            AgentAttemptFailureCode.AGENT_REFUSED,
            NodeReceiptReason.AGENT_REFUSED,
            reason,
            None,
        )

    def complete_project_verification_failure(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End the armed attempt whose granted verification never produced an exit.

        The project's command was started after this live call had already claimed;
        it then did not answer, so there is no exit code to keep and no
        `tool_redemptions` row. The attempt ends on the same
        `PROJECT_VERIFICATION_FAILED` seam a nonzero exit uses, with `verdict`
        naming why -- the declared timeout, not an invented code.

        The provider had already answered when the check went silent, so its
        steps are kept here too. Losing them on this one path would make the
        transcript's absence mean two different things -- "no executor decoded
        one" and "a verification timed out afterwards" -- and a reader could not
        tell which.
        """
        return self._judged_armed_failure(
            execution,
            AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED,
            NodeReceiptReason.PROJECT_VERIFICATION_FAILED,
            verdict,
            transcript,
        )

    def complete_candidate_unchanged(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End the armed attempt that left the tree its pin named untouched.

        No process died, no form refused anything and no check ever ran, so
        there is no exit code and no `tool_redemptions` row -- `verdict` carries
        the words of the caller that compared the two trees, and the provider's
        own answer stands in them. The attempt is terminal: a run whose builder
        produced nothing has nothing for a later node to judge, and pretending
        otherwise is how ten minutes of verification came to be spent on a
        pinned tree (#1156).
        """
        return self._judged_armed_failure(
            execution,
            AgentAttemptFailureCode.CANDIDATE_UNCHANGED,
            NodeReceiptReason.CANDIDATE_UNCHANGED,
            verdict,
            transcript,
        )

    def complete_candidate_capture_failure(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptFailed:
        """End the armed attempt whose finished work could not be kept.

        Nothing about this attempt went wrong except the last thing: the process
        answered, the schema admitted the bytes and any granted check passed.
        What failed is the keeping, so `verdict` carries the store's own words
        rather than an exit code no process produced.

        `redemption` is that passed check's own proof, and it becomes durable in
        this same write. The check really ran and really exited zero; the work
        being unkeepable afterwards says nothing about it, and discarding its
        evidence would leave an operator unable to tell a project whose tests
        pass from one whose tests were never satisfied.

        The provider's steps are kept here for a stronger reason than anywhere
        else. Once the workspace is released, the transcript is the only
        surviving evidence that this work was ever done at all.
        """
        return self._judged_armed_failure(
            execution,
            AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED,
            NodeReceiptReason.CANDIDATE_CAPTURE_FAILED,
            verdict,
            transcript,
            _proof_of_a_passed_check(redemption),
        )

    def _judged_armed_failure(
        self,
        execution: AgentAttemptExecution,
        failure: AgentAttemptFailureCode,
        token: NodeReceiptReason,
        verdict: str,
        transcript: AttemptTranscript | None,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptFailed:
        """End this run's armed current attempt under one judged ending.

        Every ending an attempt reaches *after* its claim has won -- and that
        no process exit judged -- passes through here, because which attempt may
        end is one rule: the armed current attempt of a started run, and nothing
        else. Two copies of that rule would be two chances for it to drift.
        """
        if not verdict:
            raise ValueError(f"an ending named {token.value} says why it happened")
        request = execution.request
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict("only the armed current attempt can fail")
            return _fail_current_attempt(
                connection,
                execution,
                durable,
                failure,
                node_receipt_reason(token, verdict),
                transcript=transcript,
                redemption=redemption,
            )

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult:
        with canonical_write_transaction(self._engine) as connection:
            run_record = connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == request.run_id.value)
            )
            if run_record is None:
                return AgentAttemptCancellationRunMissing()
            record = (
                connection.execute(
                    sa.select(agent_attempts).where(
                        agent_attempts.c.attempt_id == request.attempt_id.value
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                return AgentAttemptCancellationTargetMissing()
            attempt = attempt_from_record(record)
            if attempt.run_id != request.run_id:
                return AgentAttemptCancellationTargetMissing()
            existing = attempt.cancellation
            if existing is not None:
                return self._repeated_cancellation_answer(
                    connection, attempt, existing, request
                )
            if attempt.state in TERMINAL_AGENT_ATTEMPT_STATES:
                return AgentAttemptCancellationTerminalConflict()
            if attempt.state_version != request.expected_attempt_state_version:
                return AgentAttemptCancellationStale()
            if request.replacement is AgentAttemptReplacement.ONE and (
                attempt.runner_manifest_id is not None
                or attempt.attempt_ordinal != AGENT_ATTEMPT_ORDINAL
            ):
                return AgentAttemptReplacementNotAllowed()
            current_ordinal = connection.scalar(
                sa.select(sa.func.max(agent_attempts.c.attempt_ordinal)).where(
                    agent_attempts.c.node_execution_id
                    == attempt.node_execution_id.value
                )
            )
            run = load_run(connection, request.run_id)
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != attempt.node_id
                or int(current_ordinal or 0) != attempt.attempt_ordinal
            ):
                return AgentAttemptCancellationNotCurrent()
            committed = self._commit_new_cancellation(connection, attempt, request)
            if committed is None:
                return AgentAttemptCancellationStale()
            return AgentAttemptCancellationAccepted(committed, False)

    def _repeated_cancellation_answer(
        self,
        connection: Connection,
        attempt: AgentAttempt,
        existing: AgentAttemptCancellation,
        request: CancelAgentAttemptRequest,
    ) -> AgentAttemptCancellationResult:
        """A retried command reads the answer its first delivery already stamped."""
        if not existing.matches(request):
            return AgentAttemptCancellationCommandConflict()
        return AgentAttemptCancellationAccepted(
            attempt,
            attempt.state
            in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED},
            self._replacement_attempt_id(connection, attempt),
        )

    def request_run_cancellation(
        self, request: CancelRunRequest
    ) -> RunCancellationResult:
        """Resolve one operator run-cancel command against the store's own truth.

        Ordering is load-bearing (#439 Bauplan P2), not incidental:

        1. **A known command answers first, before any state gate.** The
           attempt row carrying this exact command id -- whatever state it
           reached -- is the canonical answer regardless of where the run
           stands today; a retry after a lost response must never be told
           "not cancellable" merely because the run moved on since the
           command it already answered.
        2. **The success-wins fallback.** A runner-carried success clears that
           row's cancellation columns in the same transaction that writes
           `AGENT_COMPLETED` (`_commit_success`), so the row this command
           stamped can vanish out from under it. The command's own
           `AGENT_CANCEL_REQUESTED` event survives that clearing -- events are
           never rewritten -- so a retry that misses the row reads the event
           log instead and answers from whichever terminal event followed.
        3. **Only a genuinely new command reaches the cancellability gate:**
           the run must be `STARTED`, and the node execution the operator's
           confirmation named is recomputed from durable truth rather than
           trusted, exactly like every other execution identity a CAS
           transaction in this module already recomputes (D2, #439 Bauplan).
        4. **The write itself is `request_cancellation`'s own CAS body** --
           `_commit_new_cancellation` is the one place both make it.
        """
        command_id = RunCancelCommandId.for_key(request.idempotency_key).value
        with canonical_write_transaction(self._engine) as connection:
            record = (
                connection.execute(
                    sa.select(agent_attempts).where(
                        agent_attempts.c.cancellation_command_id == command_id,
                        agent_attempts.c.run_id == request.run_id.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is not None:
                return _known_run_cancellation(
                    connection, request.run_id, attempt_from_record(record)
                )

            from_event_log = _run_cancellation_from_event_log(
                connection, request.run_id, command_id
            ) or _wait_cancellation_from_event_log(
                connection, request.run_id, command_id
            )
            if from_event_log is not None:
                return from_event_log

            run_record = connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == request.run_id.value)
            )
            if run_record is None:
                return RunCancellationRunMissing()
            run = load_run(connection, request.run_id)
            if run.state in TERMINAL_RUN_STATES:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.ALREADY_ENDED
                )
            resting_wait_run = _resting_wait_run(run)
            waiting_for_a_person = run.state in {
                RunState.WAITING_INPUT,
                RunState.WAITING_RECONCILIATION,
            }
            # A reconciliation pause keeps `waiting-for-you`: an Action's live
            # intent stands behind it, and ending the run there would abandon
            # it. So does any pause a format-3 line did not write, because
            # `WAIT_CANCELLED` is a kind only the V3 wire publishes (#668).
            if waiting_for_a_person and resting_wait_run is None:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.WAITING_FOR_YOU
                )

            live_node_execution_id = NodeExecutionId.for_node(
                run.run_id,
                run.revision_hash,
                run.current_node_id,
                run.current_round_ordinal,
            )
            if live_node_execution_id != request.expected_node_execution_id:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.BETWEEN_NODES
                )

            if resting_wait_run is not None:
                return _cancel_resting_wait(
                    connection, resting_wait_run, live_node_execution_id, command_id
                )

            graph = load_graph(connection, run.revision_hash)
            current_node = graph.node(run.current_node_id)
            if not isinstance(current_node, AgentNodeV3):
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.NODE_RUNS_NO_AGENT
                )

            current_record = (
                connection.execute(
                    sa.select(agent_attempts)
                    .where(
                        agent_attempts.c.node_execution_id
                        == live_node_execution_id.value
                    )
                    .order_by(agent_attempts.c.attempt_ordinal.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if current_record is None:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.BETWEEN_NODES
                )
            current_attempt = attempt_from_record(current_record)
            if current_attempt.state in TERMINAL_AGENT_ATTEMPT_STATES:
                # The run is still STARTED -- only its current node's attempt
                # ended, so `already-ended` would name the run dead when it is
                # about to move on. That is the exact standing the projection
                # shows the operator as `between-nodes`; both paths speak one
                # honest sentence for one durable state (#439 P6).
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.BETWEEN_NODES
                )
            if current_attempt.cancellation is not None:
                # Some other command -- the attempt route, or an earlier
                # idempotency key -- already owns this attempt's cancellation.
                return RunCancellationCommandConflict()

            cancel_request = CancelAgentAttemptRequest(
                request.run_id,
                current_attempt.attempt_id,
                command_id,
                current_attempt.state_version,
                AgentAttemptReplacement.NONE,
            )
            committed = self._commit_new_cancellation(
                connection, current_attempt, cancel_request
            )
            if committed is None:
                return RunCancellationCommandConflict()
            return RunCancellationAccepted(committed)

    def _commit_new_cancellation(
        self,
        connection: Any,
        attempt: AgentAttempt,
        request: CancelAgentAttemptRequest,
    ) -> AgentAttempt | None:
        """Stamp `CANCEL_REQUESTED` under one command and enqueue its cleanup.

        The CAS body `request_cancellation` and `request_run_cancellation`
        share: both resolve which non-terminal attempt, at which version, a
        genuinely new command targets before calling this, so from here the
        write is the same either way. `None` means the CAS lost its race --
        the two callers name that in their own, different vocabularies
        (`Stale` for a client-supplied version; `CommandConflict` for a
        server-resolved one), so the naming stays here.

        Every accepted command -- local-process and runner-lease alike --
        enqueues the one carrier-aware cancellation workflow in this same
        transaction. A runner-lease-bound attempt used to return here with
        nothing enqueued, which left an operator's run-cancel stamped
        `CANCEL_REQUESTED` with no owner to converge it (#584); the workflow
        itself now dispatches on the durable carrier.
        """
        workflow_id = cancellation_workflow_id_for(request)
        updated = connection.execute(
            agent_attempts.update()
            .where(
                agent_attempts.c.attempt_id == attempt.attempt_id.value,
                agent_attempts.c.state == attempt.state.value,
                agent_attempts.c.state_version == attempt.state_version,
                agent_attempts.c.cancellation_command_id.is_(None),
            )
            .values(
                state=AgentAttemptState.CANCEL_REQUESTED.value,
                state_version=attempt.state_version + 1,
                cancellation_command_id=request.command_id,
                cancellation_expected_state_version=(
                    request.expected_attempt_state_version
                ),
                replacement=request.replacement.value,
                redrive_state=AgentAttemptRedriveState.PENDING.value,
                cancellation_workflow_id=workflow_id,
            )
        )
        if updated.rowcount != 1:
            return None
        accepted = _load_attempt(connection, attempt.attempt_id)
        _insert_attempt_event(
            connection,
            accepted,
            RunEventKind.AGENT_CANCEL_REQUESTED,
            command=accepted.cancellation,
        )
        if self._application_version is None:
            raise RunTransitionConflict(
                "cancellation submission requires the runtime application version"
            )
        self._enqueue_workflow(
            connection,
            CANCELLATION_WORKFLOW_NAME,
            workflow_id,
            self._application_version,
            attempt.run_id.value,
            attempt.attempt_id.value,
            request.command_id,
        )
        return accepted

    def _enqueue_workflow(
        self,
        connection: Connection,
        workflow_name: str,
        workflow_id: str,
        application_version: str,
        *arguments: str,
    ) -> None:
        """Enqueue one workflow in the caller's transaction, so both land or neither."""
        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
            options: EnqueueOptions = {
                "workflow_name": workflow_name,
                "queue_name": QUEUE_NAME,
                "workflow_id": workflow_id,
                "app_version": application_version,
            }
            client.enqueue_in_transaction(connection, options, *arguments)
        finally:
            client.destroy()

    def _submit_replacement_attempt(
        self, connection: Connection, attempt: AgentAttempt
    ) -> AgentAttemptId:
        """Prepare the attempt that takes over this execution and enqueue its run."""
        replacement = AgentAttempt(
            AgentAttemptId.for_execution(
                attempt.node_execution_id,
                attempt.request_hash,
                REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
            ),
            attempt.node_execution_id,
            attempt.request_hash,
            attempt.executor_operational_identity,
            attempt.run_id,
            attempt.workflow_revision_hash,
            attempt.node_id,
            REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
            AgentAttemptState.PREPARED,
            0,
        )
        connection.execute(agent_attempts.insert().values(_attempt_values(replacement)))
        record_attempt_started(connection, replacement.attempt_id.value)
        if self._application_version is None:
            raise RunTransitionConflict(
                "replacement submission requires the runtime application version"
            )
        self._enqueue_workflow(
            connection,
            REPLACEMENT_WORKFLOW_NAME,
            replacement_workflow_id_for(replacement.attempt_id),
            self._application_version,
            replacement.attempt_id.value,
        )
        return replacement.attempt_id

    def attest_cancellation_cleanup(
        self,
        request: CancelAgentAttemptRequest,
        disposition: AgentAttemptCancellationDisposition,
        process_owner_id: AgentProcessOwnerId | None,
        watchdog_generation_id: WatchdogGenerationId | None,
    ) -> AgentAttemptCancellationAccepted:
        with canonical_write_transaction(self._engine) as connection:
            attempt = _load_attempt(connection, request.attempt_id)
            if attempt.runner_manifest_id is not None:
                raise RunTransitionConflict(
                    "runner-bound cancellation cleanup requires Runner evidence"
                )
            cancellation = attempt.cancellation
            if cancellation is None or not cancellation.matches(request):
                raise RunTransitionConflict(
                    "cleanup attestation differs from its cancellation command"
                )
            if attempt.state in {
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                if cancellation.disposition is not disposition:
                    raise RunTransitionConflict(
                        "cleanup retry differs from durable disposition"
                    )
                return AgentAttemptCancellationAccepted(
                    attempt,
                    True,
                    self._replacement_attempt_id(connection, attempt),
                )
            if attempt.state is not AgentAttemptState.CANCEL_REQUESTED:
                raise RunTransitionConflict("only a requested cancellation can attest")
            if (
                attempt.process_owner_id != process_owner_id
                or attempt.watchdog_generation_id != watchdog_generation_id
            ):
                raise RunTransitionConflict(
                    "cleanup attestation differs from durable owner generation"
                )
            terminal_state = (
                AgentAttemptState.INTERRUPTED
                if disposition
                is AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH
                else AgentAttemptState.CANCELLED
            )
            terminal_cancellation = AgentAttemptCancellation(
                cancellation.command_id,
                cancellation.expected_attempt_state_version,
                cancellation.replacement,
                AgentAttemptRedriveState.CLEANUP_ATTESTED,
                disposition,
            )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                    agent_attempts.c.state_version == attempt.state_version,
                    agent_attempts.c.cancellation_command_id == request.command_id,
                )
                .values(
                    state=terminal_state.value,
                    state_version=attempt.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.CLEANUP_ATTESTED.value,
                    process_owner_id=(
                        None if process_owner_id is None else process_owner_id.value
                    ),
                    watchdog_generation_id=(
                        None
                        if watchdog_generation_id is None
                        else watchdog_generation_id.value
                    ),
                    redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED.value,
                    cancellation_disposition=disposition.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("cleanup attestation lost its attempt CAS")
            record_attempt_ended(connection, attempt.attempt_id.value)
            terminal = _load_attempt(connection, attempt.attempt_id)
            replacement_attempt_id = (
                self._submit_replacement_attempt(connection, attempt)
                if cancellation.replacement is AgentAttemptReplacement.ONE
                else None
            )
            _insert_attempt_event(
                connection,
                terminal,
                _CANCELLATION_END_EVENT_BY_STATE[terminal_state],
                command=terminal_cancellation,
                replacement_attempt_id=replacement_attempt_id,
            )
            _lift_run_under_operator_cancel(connection, terminal)
            return AgentAttemptCancellationAccepted(
                terminal, True, replacement_attempt_id
            )

    def mark_cancellation_owner_not_local(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            attempt = _load_attempt(connection, request.attempt_id)
            cancellation = attempt.cancellation
            if cancellation is None or not cancellation.matches(request):
                raise RunTransitionConflict(
                    "owner redrive differs from its cancellation command"
                )
            if cancellation.redrive_state is AgentAttemptRedriveState.OWNER_NOT_LOCAL:
                return attempt
            if (
                attempt.state is not AgentAttemptState.CANCEL_REQUESTED
                or cancellation.redrive_state is not AgentAttemptRedriveState.PENDING
            ):
                raise RunTransitionConflict(
                    "only a pending cancellation can lose its local owner"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                    agent_attempts.c.state_version == attempt.state_version,
                    agent_attempts.c.redrive_state
                    == AgentAttemptRedriveState.PENDING.value,
                )
                .values(
                    state_version=attempt.state_version + 1,
                    redrive_state=AgentAttemptRedriveState.OWNER_NOT_LOCAL.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("owner redrive lost its attempt CAS")
            return _load_attempt(connection, attempt.attempt_id)

    @staticmethod
    def _replacement_attempt_id(
        connection: Any, attempt: AgentAttempt
    ) -> AgentAttemptId | None:
        if (
            attempt.cancellation is None
            or attempt.cancellation.replacement is AgentAttemptReplacement.NONE
        ):
            return None
        value = connection.scalar(
            sa.select(agent_attempts.c.attempt_id).where(
                agent_attempts.c.node_execution_id == attempt.node_execution_id.value,
                agent_attempts.c.attempt_ordinal == 2,
            )
        )
        return None if value is None else AgentAttemptId(str(value))
