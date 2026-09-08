from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, assert_never

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.run_transitions import (
    commit_reconciliation_required,
    commit_reconciliation_resolved,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_fork_effect_fences,
    run_fork_reused_nodes,
    run_forks,
    runs,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.uncontinuable_runs import (
    LIVE_DRIVER_WORKFLOW_STATUSES,
    live_driver_workflow_ids,
)
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    fork_bootstrap_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effects import (
    EFFECT_INTENT_VERSION_ABANDONED,
    EFFECT_INTENT_VERSION_CONFIRMED_INITIAL,
    EFFECT_INTENT_VERSION_CONFIRMED_RECONCILED,
    EFFECT_INTENT_VERSION_INITIAL,
    EFFECT_INTENT_VERSION_RECONCILING,
    EFFECT_INTENT_VERSION_WAITING,
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectOutcome,
    EffectReceipt,
    EffectReceiptReference,
    EffectRequestHash,
    EffectResult,
    EffectUnknownOutcome,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    PerformedEffect,
    ReadbackPhase,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
    UnknownOutcomeReason,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    logical_effect_key_for_node,
    logical_effect_key_for_work_item_claim,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.ports.effects import EffectAdapter

_LOG = logging.getLogger("atelier2")

type EncodedEffectResolution = dict[str, str | None]


class DurableEffectConflict(RuntimeError):
    """Durable rows do not form the exact effect transition expected."""


def intent_from_record(record: Mapping[Any, Any]) -> EffectIntent:
    request = CanonicalRequest.from_durable_record(
        bytes(record["canonical_request"]),
        EffectRequestHash(str(record["request_hash"])),
    )
    return EffectIntent(
        EffectBinding(
            LogicalEffectKey(str(record["logical_key"])),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            AdapterRevision(str(record["adapter_revision"])),
            EffectDestination(str(record["destination_identity"])),
            AdapterOperationalIdentity(str(record["adapter_operational_identity"])),
            AdapterOperationName(str(record["operation_name"])),
        ),
        request,
    )


def intent_snapshot_from_record(record: Mapping[Any, Any]) -> EffectIntentSnapshot:
    return EffectIntentSnapshot(
        intent_from_record(record),
        EffectIntentState(str(record["state"])),
        EffectIntentStateVersion(int(record["state_version"])),
    )


def receipt_from_record(record: Mapping[Any, Any]) -> EffectReceipt:
    command_id = record["reconcile_command_id"]
    return EffectReceipt(
        intent=intent_from_record(record),
        effect_id=EffectId(str(record["effect_id"])),
        result=EffectResult.from_durable_record(
            bytes(record["result"]), Sha256Hash(str(record["result_hash"]))
        ),
        confirmation_source=ConfirmationSource(str(record["confirmation_source"])),
        reconcile_command_id=(
            None if command_id is None else ReconcileCommandId(str(command_id))
        ),
        source_receipt=_fork_source_reference(record),
    )


def _fork_source_reference(record: Mapping[Any, Any]) -> EffectReceiptReference | None:
    values = (
        record.get("fork_source_logical_key"),
        record.get("fork_source_run_id"),
        record.get("fork_source_workflow_revision_hash"),
        record.get("fork_source_result_hash"),
    )
    if any(value is None for value in values):
        if not all(value is None for value in values):
            raise ValueError("fork receipt source identity is incomplete")
        return None
    return EffectReceiptReference(
        LogicalEffectKey(str(values[0])),
        RunId(str(values[1])),
        WorkflowRevisionHash(str(values[2])),
        Sha256Hash(str(values[3])),
    )


def command_from_record(
    record: Mapping[Any, Any], intent: EffectIntent
) -> ReconcileCommand:
    found_effect_id = record["found_effect_id"]
    if found_effect_id is None:
        determination = OperatorAuthoritativeAbsence()
    else:
        determination = OperatorFoundEffect(
            EffectId(str(found_effect_id)),
            EffectResult.from_durable_record(
                bytes(record["found_result"]),
                Sha256Hash(str(record["found_result_hash"])),
            ),
        )
    return ReconcileCommand(
        ReconcileCommandId(str(record["command_id"])),
        intent.reference,
        EffectIntentStateVersion(int(record["expected_intent_version"])),
        ReconcileActor(str(record["actor"])),
        str(record["evidence"]),
        determination,
    )


def command_snapshot_from_record(
    record: Mapping[Any, Any], intent: EffectIntent
) -> ReconcileCommandSnapshot:
    return ReconcileCommandSnapshot(
        command_from_record(record, intent),
        ReconcileCommandState(str(record["state"])),
    )


def load_intent(session: Any, logical_key: str, revision_hash: str) -> EffectIntent:
    intent_record = (
        session.execute(
            sa.select(effect_intents).where(effect_intents.c.logical_key == logical_key)
        )
        .mappings()
        .one_or_none()
    )
    if intent_record is None:
        raise DurableEffectConflict("durable effect workflow has no prepared intent")
    snapshot = intent_snapshot_from_record(intent_record)
    if snapshot.intent.binding.workflow_revision_hash.value != revision_hash:
        raise DurableEffectConflict("effect workflow revision binding changed")
    run_revision = session.scalar(
        sa.select(runs.c.revision_hash).where(
            runs.c.run_id == snapshot.intent.binding.run_id.value
        )
    )
    if run_revision != revision_hash:
        raise DurableEffectConflict("run and effect intent revisions disagree")
    return snapshot.intent


def encode_found(
    performed: PerformedEffect,
    source: ConfirmationSource,
    command_id: ReconcileCommandId | None = None,
    source_receipt: EffectReceiptReference | None = None,
) -> EncodedEffectResolution:
    return {
        "outcome": "FOUND",
        "effect_id": performed.effect_id.value,
        "result": performed.result.payload.hex(),
        "result_hash": performed.result.payload_hash.value,
        "confirmation_source": source.value,
        "reconcile_command_id": None if command_id is None else command_id.value,
        "fork_source_logical_key": (
            None if source_receipt is None else source_receipt.logical_key.value
        ),
        "fork_source_run_id": None
        if source_receipt is None
        else source_receipt.run_id.value,
        "fork_source_workflow_revision_hash": (
            None
            if source_receipt is None
            else source_receipt.workflow_revision_hash.value
        ),
        "fork_source_result_hash": (
            None if source_receipt is None else source_receipt.result_hash.value
        ),
    }


def encode_readback(
    readback: EffectReceipt | EffectAbsence | EffectUnknownOutcome,
) -> EncodedEffectResolution:
    if isinstance(readback, EffectReceipt):
        return encode_found(
            PerformedEffect(readback.effect_id, readback.result),
            readback.confirmation_source,
            readback.reconcile_command_id,
            readback.source_receipt,
        )
    if isinstance(readback, EffectUnknownOutcome) and readback.reason is not None:
        _log_unknown_outcome_entering_reconciliation(readback.reason)
    return {"outcome": readback.outcome.value}


def _log_unknown_outcome_entering_reconciliation(reason: UnknownOutcomeReason) -> None:
    """The one record of why, since durable state carries only that it did not.

    The reason itself is not persisted (issue #1210 names the durable event
    field as its own follow-up); this line is the only place an operator
    diagnosing a second reconciliation can read what the destination said.
    """

    _LOG.warning(
        "An adapter readback stayed unknown before reconciliation: %s",
        reason.detail,
        extra={
            "event": "effect_unknown_outcome_entered_reconciliation",
            "failure_code": reason.failure_code,
            "duration_milliseconds": reason.duration_milliseconds,
            "detail": reason.detail,
        },
    )


def decode_found(intent: EffectIntent, encoded: Mapping[str, Any]) -> EffectReceipt:
    result = EffectResult.from_durable_record(
        bytes.fromhex(str(encoded["result"])),
        Sha256Hash(str(encoded["result_hash"])),
    )
    command_id = encoded.get("reconcile_command_id")
    return EffectReceipt(
        intent,
        effect_id=EffectId(str(encoded["effect_id"])),
        result=result,
        confirmation_source=ConfirmationSource(str(encoded["confirmation_source"])),
        reconcile_command_id=(
            None if command_id is None else ReconcileCommandId(str(command_id))
        ),
        source_receipt=_fork_source_reference(encoded),
    )


def fork_fenced_resolution(
    session: Any, logical_key: str, revision_hash: str
) -> EncodedEffectResolution | None:
    """Resolve a successor effect from its source fence, before any adapter call."""

    intent = load_intent(session, logical_key, revision_hash)
    run_record = (
        session.execute(
            sa.select(
                runs.c.bootstrap_workflow_id,
                runs.c.current_node_id,
                runs.c.current_round_ordinal,
            ).where(runs.c.run_id == intent.binding.run_id.value)
        )
        .mappings()
        .one()
    )
    fork_record = (
        session.execute(
            sa.select(run_forks).where(
                run_forks.c.successor_run_id == intent.binding.run_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    fork_workflow_id = fork_bootstrap_workflow_id_for(intent.binding.run_id)
    if fork_record is None:
        if str(run_record["bootstrap_workflow_id"]) == fork_workflow_id:
            raise DurableEffectConflict(
                "fork successor is visible without its committed fence transaction"
            )
        return None
    if (
        str(run_record["bootstrap_workflow_id"]) != fork_workflow_id
        or str(fork_record["workflow_revision_hash"]) != revision_hash
    ):
        raise DurableEffectConflict("fork successor header disagrees with its run")
    node_id = str(run_record["current_node_id"])
    round_ordinal = int(run_record["current_round_ordinal"])
    if (
        logical_effect_key_for_node(
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            node_id,
            round_ordinal,
        ).value
        != logical_key
    ):
        raise DurableEffectConflict("fork effect intent is not at the current node")
    fence = (
        session.execute(
            sa.select(run_fork_effect_fences).where(
                run_fork_effect_fences.c.successor_run_id
                == intent.binding.run_id.value,
                run_fork_effect_fences.c.node_id == node_id,
                run_fork_effect_fences.c.round_ordinal == round_ordinal,
            )
        )
        .mappings()
        .one_or_none()
    )
    if fence is None:
        source_run_id = str(fork_record["origin_run_id"])
        source_revision_hash = revision_hash
        reused = (
            session.execute(
                sa.select(run_fork_reused_nodes).where(
                    run_fork_reused_nodes.c.successor_run_id == source_run_id,
                    run_fork_reused_nodes.c.node_id == node_id,
                    run_fork_reused_nodes.c.round_ordinal == round_ordinal,
                )
            )
            .mappings()
            .one_or_none()
        )
        if reused is not None:
            source_run_id = str(reused["source_run_id"])
            source_revision_hash = str(reused["source_workflow_revision_hash"])
        source_logical_key = logical_effect_key_for_node(
            RunId(source_run_id),
            WorkflowRevisionHash(source_revision_hash),
            node_id,
            round_ordinal,
        )
        source_receipt_exists = session.scalar(
            sa.select(sa.literal(True)).where(
                sa.exists().where(
                    effect_receipts.c.logical_key == source_logical_key.value,
                    effect_receipts.c.run_id == source_run_id,
                    effect_receipts.c.workflow_revision_hash == source_revision_hash,
                )
            )
        )
        if source_receipt_exists:
            return {"outcome": EffectOutcome.UNKNOWN.value}
        return None
    source_record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == str(fence["source_logical_key"]),
                effect_receipts.c.run_id == str(fence["source_run_id"]),
                effect_receipts.c.workflow_revision_hash
                == str(fence["source_workflow_revision_hash"]),
                effect_receipts.c.result_hash == str(fence["source_result_hash"]),
            )
        )
        .mappings()
        .one_or_none()
    )
    if source_record is None:
        raise DurableEffectConflict("fork effect fence source receipt is missing")
    source = receipt_from_record(source_record)
    exact = (
        source.intent.request == intent.request
        and source.intent.binding.adapter_revision == intent.binding.adapter_revision
        and source.intent.binding.destination == intent.binding.destination
        and source.intent.binding.adapter_operational_identity
        == intent.binding.adapter_operational_identity
    )
    if not exact:
        return {"outcome": EffectOutcome.UNKNOWN.value}
    source_reference = EffectReceiptReference(
        source.intent.binding.logical_key,
        source.intent.binding.run_id,
        source.intent.binding.workflow_revision_hash,
        source.result.payload_hash,
    )
    return encode_found(
        PerformedEffect(source.effect_id, source.result),
        ConfirmationSource.FORK_REFERENCE,
        source_receipt=source_reference,
    )


def observe_adapter(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
) -> EncodedEffectResolution:
    """Read the destination before this intent has sent anything.

    This is the observing step of `durable_effect`, and it runs before that
    workflow's own resolving step ever reaches an adapter's `execute`. A
    recovery replays the outcome this step recorded rather than reading again,
    so nothing was ever sent when this read happens -- which is exactly what
    lets a destination holding nothing be the authoritative absence that
    licenses the send (ADR 0010 decision 5, as amended 2026-09-05).
    """

    intent = load_intent(session, logical_key, revision_hash)
    readback = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
    intent.authorize_adapter_readback(readback, ReadbackPhase.BEFORE_SEND)
    return encode_readback(readback)


def observe_adapter_with_fork_fence(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
) -> EncodedEffectResolution:
    """Apply the cross-run fence before allowing an adapter readback or execution."""

    fenced = fork_fenced_resolution(session, logical_key, revision_hash)
    if fenced is not None:
        return fenced
    return observe_adapter(session, adapter, logical_key, revision_hash)


def resolve_observation(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
    observed: Mapping[str, Any],
    authorized_command_id: ReconcileCommandId | None = None,
) -> EncodedEffectResolution:
    intent = load_intent(session, logical_key, revision_hash)
    if observed["outcome"] == "FOUND":
        if authorized_command_id is None:
            return dict(observed)
        found = decode_found(intent, observed)
        return encode_found(
            PerformedEffect(found.effect_id, found.result),
            ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
            authorized_command_id,
        )
    if observed["outcome"] == "UNKNOWN" and authorized_command_id is None:
        return {"outcome": "UNKNOWN"}
    performed = adapter.execute(intent)
    if isinstance(performed, EffectUnknownOutcome):
        intent.authorize_adapter_readback(performed, ReadbackPhase.AFTER_SEND)
        return encode_readback(performed)
    return encode_found(
        performed,
        (
            ConfirmationSource.ADAPTER_EXECUTION
            if authorized_command_id is None
            else ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION
        ),
        authorized_command_id,
    )


def observe_reconcile_command(
    session: Any,
    adapter: EffectAdapter,
    command_id: str,
    revision_hash: str,
) -> EncodedEffectResolution:
    command_record = (
        session.execute(
            sa.select(reconcile_commands).where(
                reconcile_commands.c.command_id == command_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if command_record is None:
        raise DurableEffectConflict("reconcile workflow has no durable command")
    intent = load_intent(session, str(command_record["logical_key"]), revision_hash)
    command_snapshot = command_snapshot_from_record(command_record, intent)
    intent_record = (
        session.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == intent.binding.logical_key.value
            )
        )
        .mappings()
        .one()
    )
    intent_snapshot = intent_snapshot_from_record(intent_record)
    if (
        command_snapshot.state is not ReconcileCommandState.PENDING
        or intent_snapshot.state is not EffectIntentState.RECONCILING
        or intent_snapshot.state_version != EFFECT_INTENT_VERSION_RECONCILING
        or intent_record["reconciliation_owner_command_id"] != command_id
    ):
        raise DurableEffectConflict(
            "reconcile workflow does not own its PENDING command and intent"
        )
    determination = command_snapshot.command.determination
    if isinstance(determination, OperatorFoundEffect):
        observed = encode_found(
            PerformedEffect(determination.effect_id, determination.result),
            ConfirmationSource.OPERATOR_FOUND,
            command_snapshot.command.command_id,
        )
    elif isinstance(determination, OperatorAuthoritativeAbsence):
        # An intent only reaches reconciliation behind an unknown outcome, and
        # an ambiguous send is one of the two ways to get one, so this read may
        # not read a silent destination as an absence. What licenses the
        # execution that follows is the operator's own determination.
        readback = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
        intent.authorize_adapter_readback(readback, ReadbackPhase.AFTER_SEND)
        observed = encode_readback(readback)
        observed["operator_authorized"] = command_id
    else:
        assert_never(determination)
    observed["logical_key"] = intent.binding.logical_key.value
    return observed


def _receipt_values(receipt: EffectReceipt) -> dict[str, object]:
    binding = receipt.intent.binding
    source = receipt.source_receipt
    command_id = receipt.reconcile_command_id
    return {
        "logical_key": binding.logical_key.value,
        "run_id": binding.run_id.value,
        "canonical_request": receipt.intent.request.payload,
        "request_hash": receipt.intent.request.request_hash.value,
        "workflow_revision_hash": binding.workflow_revision_hash.value,
        "adapter_revision": binding.adapter_revision.value,
        "destination_identity": binding.destination.value,
        "adapter_operational_identity": binding.adapter_operational_identity.value,
        "operation_name": binding.operation_name.value,
        "effect_id": receipt.effect_id.value,
        "result": receipt.result.payload,
        "result_hash": receipt.result.payload_hash.value,
        "confirmation_source": receipt.confirmation_source.value,
        "reconcile_command_id": None if command_id is None else command_id.value,
        "fork_source_logical_key": None if source is None else source.logical_key.value,
        "fork_source_run_id": None if source is None else source.run_id.value,
        "fork_source_workflow_revision_hash": (
            None if source is None else source.workflow_revision_hash.value
        ),
        "fork_source_result_hash": None if source is None else source.result_hash.value,
    }


@dataclass(frozen=True)
class _ConfirmationDoor:
    name: str
    admitted_sources: tuple[ConfirmationSource, ...]
    expected_state: EffectIntentState
    expected_version: EffectIntentStateVersion
    confirmed_version: EffectIntentStateVersion


_INITIAL_CONFIRMATION = _ConfirmationDoor(
    "initial",
    (
        ConfirmationSource.ADAPTER_READBACK,
        ConfirmationSource.ADAPTER_EXECUTION,
        ConfirmationSource.FORK_REFERENCE,
    ),
    EffectIntentState.PREPARED,
    EFFECT_INTENT_VERSION_INITIAL,
    EFFECT_INTENT_VERSION_CONFIRMED_INITIAL,
)
_RECONCILED_CONFIRMATION = _ConfirmationDoor(
    "reconciliation",
    (
        ConfirmationSource.OPERATOR_FOUND,
        ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
    ),
    EffectIntentState.RECONCILING,
    EFFECT_INTENT_VERSION_RECONCILING,
    EFFECT_INTENT_VERSION_CONFIRMED_RECONCILED,
)


def commit_resolution(
    session: Any,
    logical_key: str,
    revision_hash: str,
    resolved: Mapping[str, Any],
    command_id: ReconcileCommandId | None = None,
) -> RunState:
    intent = load_intent(session, logical_key, revision_hash)
    if resolved["outcome"] == "UNKNOWN":
        if command_id is not None:
            _reopen_reconciliation(
                session, intent.binding.logical_key, command_id.value
            )
            return RunState.WAITING_RECONCILIATION
        intent_update = session.execute(
            effect_intents.update()
            .where(
                effect_intents.c.logical_key == logical_key,
                effect_intents.c.state == EffectIntentState.PREPARED.value,
                effect_intents.c.state_version == EFFECT_INTENT_VERSION_INITIAL.value,
            )
            .values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=EFFECT_INTENT_VERSION_WAITING.value,
            )
        )
        current = load_run(session, intent.binding.run_id)
        commit_reconciliation_required(
            session,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            current.current_node_id,
            intent.request.payload,
            current.current_round_ordinal,
        )
        if intent_update.rowcount != 1:
            raise DurableEffectConflict("UNKNOWN must advance one prepared run")
        return RunState.WAITING_RECONCILIATION

    receipt = decode_found(intent, resolved)
    door = _INITIAL_CONFIRMATION if command_id is None else _RECONCILED_CONFIRMATION
    if (
        receipt.confirmation_source not in door.admitted_sources
        or receipt.reconcile_command_id != command_id
    ):
        raise DurableEffectConflict(f"{door.name} confirmation has invalid provenance")

    session.execute(
        effect_receipts.insert()
        .prefix_with("OR IGNORE")
        .values(_receipt_values(receipt))
    )
    durable_receipt_record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key
            )
        )
        .mappings()
        .one()
    )
    if receipt_from_record(durable_receipt_record) != receipt:
        raise DurableEffectConflict("durable receipt differs from exact resolution")

    intent_update = session.execute(
        effect_intents.update()
        .where(
            effect_intents.c.logical_key == logical_key,
            effect_intents.c.workflow_revision_hash == revision_hash,
            effect_intents.c.state == door.expected_state.value,
            effect_intents.c.state_version == door.expected_version.value,
            effect_intents.c.reconciliation_owner_command_id
            == (None if command_id is None else command_id.value),
        )
        .values(
            state=EffectIntentState.CONFIRMED.value,
            state_version=door.confirmed_version.value,
            reconciliation_owner_command_id=None,
        )
    )
    if intent_update.rowcount != 1:
        raise DurableEffectConflict(
            "effect confirmation requires one exact run binding"
        )
    if command_id is not None:
        command_update = session.execute(
            reconcile_commands.update()
            .where(
                reconcile_commands.c.command_id == command_id.value,
                reconcile_commands.c.logical_key == logical_key,
                reconcile_commands.c.state == ReconcileCommandState.PENDING.value,
            )
            .values(state=ReconcileCommandState.APPLIED.value)
        )
        if command_update.rowcount != 1:
            raise DurableEffectConflict("confirmation must apply its owning command")
        current = load_run(session, intent.binding.run_id)
        commit_reconciliation_resolved(
            session,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            current.current_node_id,
            intent.binding.logical_key,
            receipt.result.payload,
            receipt.result.payload_hash,
            current.current_round_ordinal,
        )
    return RunState.STARTED


# DBOS owns this table and these tokens; the sweep below only reads them, and
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


def converge_driverless_effect_intents(
    engine: Engine, application_version: str
) -> tuple[LogicalEffectKey, ...]:
    """Give every intent no workflow is going to move its honest ending.

    A prepared effect moves because `durable_effect` drives it, and a
    reconciling one because the workflow of its owning command does. When the
    adapter raises -- a GitHub 500, a timeout -- that workflow ends in a
    terminal error status nothing replays, the intent stands PREPARED or
    RECONCILING forever, the operator door refuses it, and the run is frozen
    mid-word (#628). A prepared intent whose effect workflow was never even
    enqueued, because the Action node workflow that owed that enqueue ended
    terminally first, stands just as frozen and names no workflow at all
    (#646). A workflow left PENDING, ENQUEUED, or DELAYED under a retired
    `application_version` is frozen the same way: DBOS will never resume it
    under a version this instance no longer runs (#707). This is the restart's
    answer, the same one `converge_driverless_attempts` gives armed attempts:
    each such intent goes to the operator door at WAITING_RECONCILIATION, and
    never to an invented absence -- routing to the operator is exactly what an
    in-band UNKNOWN readback does (ADR 0010). Where the run itself has already
    ended there is no door left to open, and the intent is abandoned instead
    (#705). Answers with the intents converged here.
    """

    return tuple(
        logical_key
        for logical_key in _driverless_effect_intents(engine, application_version)
        if _converge_intent(engine, logical_key, application_version)
    )


class _DriverlessConvergence(Enum):
    """The ending owed to one intent no durable workflow will move again."""

    RECONCILIATION_DOOR = auto()
    """A prepared intent on a live run: lift that run to the operator's door."""

    ABANDONMENT = auto()
    """A prepared intent on a run that ended without it: write that ending."""

    REOPENED_DOOR = auto()
    """A reconciling intent whose command died: put it back behind the door."""


def _driverless_effect_intents(
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
            if _driverless_convergence(connection, record, application_version)
            is not None
        )


def _converge_intent(
    engine: Engine, logical_key: LogicalEffectKey, application_version: str
) -> bool:
    with canonical_write_transaction(engine) as connection:
        record = (
            connection.execute(
                sa.select(effect_intents).where(
                    effect_intents.c.logical_key == logical_key.value
                )
            )
            .mappings()
            .one_or_none()
        )
        if record is None:
            return False
        convergence = _driverless_convergence(connection, record, application_version)
        match convergence:
            case None:
                return False
            case _DriverlessConvergence.RECONCILIATION_DOOR:
                # The exact transition an in-band UNKNOWN readback commits:
                # intent to WAITING_RECONCILIATION, run lifted under its
                # ACTION_RECONCILIATION_REQUIRED event. An effect whose
                # workflow raised, or was never enqueued at all, is an outcome
                # nobody observed, so it routes to the operator and is never
                # turned into an absence.
                commit_resolution(
                    connection,
                    logical_key.value,
                    str(record["workflow_revision_hash"]),
                    {"outcome": EffectOutcome.UNKNOWN.value},
                )
            case _DriverlessConvergence.ABANDONMENT:
                _abandon_intent(connection, logical_key)
            case _DriverlessConvergence.REOPENED_DOOR:
                _reopen_reconciliation(
                    connection,
                    logical_key,
                    str(record["reconciliation_owner_command_id"]),
                )
        return True


def _driverless_convergence(
    connection: Connection, record: Mapping[Any, Any], application_version: str
) -> _DriverlessConvergence | None:
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
        return _DriverlessConvergence.REOPENED_DOOR
    return None


def _prepared_intent_convergence(
    connection: Connection, record: Mapping[Any, Any], application_version: str
) -> _DriverlessConvergence | None:
    """A prepared intent has two possible drivers, one of them not written yet.

    `durable_effect` resolves it, so its raised or version-stranded ending is
    the usual answer (#628, #707). Before that workflow exists there is still
    a driver: the Action node workflow commits the intent and enqueues the
    effect in the same step, so an absent effect row normally means recovery
    is about to write it. Only when that node workflow itself is dead does
    nobody owe this intent anything -- the window where it would stand
    PREPARED forever under no row the effect-workflow read could even name
    (#646).

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
) -> _DriverlessConvergence | None:
    """Which ending this intent's own run still leaves open.

    A STARTED run is what the in-band UNKNOWN transition lifts, so it takes the
    door. A run that has ended cannot be lifted at all -- its terminal word and
    hash are written -- and before ABANDONED existed that left the intent
    standing PREPARED forever behind a door refusing it (#705). Any other state
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
        return _DriverlessConvergence.RECONCILIATION_DOOR
    if state in TERMINAL_RUN_STATES:
        return _DriverlessConvergence.ABANDONMENT
    return None


def _abandon_intent(connection: Connection, logical_key: LogicalEffectKey) -> None:
    """Write the run's ending onto the intent nobody will ever move.

    Only the intent row moves. No run event is appended: the run's terminal
    event is already the last word its hash folds over, and an abandonment
    observed nothing that could be attested. The CAS on PREPARED at its initial
    version is what keeps this safe beside a driver that comes back to life --
    a workflow resolving the intent after all loses its own update and raises,
    rather than writing a receipt over an ending.
    """

    abandoned = connection.execute(
        effect_intents.update()
        .where(
            effect_intents.c.logical_key == logical_key.value,
            effect_intents.c.state == EffectIntentState.PREPARED.value,
            effect_intents.c.state_version == EFFECT_INTENT_VERSION_INITIAL.value,
        )
        .values(
            state=EffectIntentState.ABANDONED.value,
            state_version=EFFECT_INTENT_VERSION_ABANDONED.value,
        )
    )
    if abandoned.rowcount != 1:
        raise DurableEffectConflict("abandonment requires one prepared intent")


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

    A terminal error status is the ordinary answer (#628); `terminal_statuses`
    lets a node-workflow caller widen that set to SUCCESS, since a node's own
    successful resolution discharges it just as finally. A workflow still
    PENDING, ENQUEUED, or DELAYED under a retired `application_version` is
    just as dead: DBOS scopes recovery to the version that enqueued it, so a
    deploy that retires that version strands the workflow exactly as if it
    had raised (#707). An absent row is neither -- it was never written, and
    who owes it decides what that means, not this predicate.
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


def _reopen_reconciliation(
    connection: Connection, logical_key: LogicalEffectKey, owner_command_id: str
) -> None:
    """Close the dead command and put the intent back behind the open door.

    The command ends REJECTED_CONFLICT -- the closest word the persisted
    vocabulary has for "recorded, but it did not take effect" -- so a retry of
    the exact same command answers rejected instead of forever pending, and
    the operator decides again with fresh evidence. The state version returns
    to the WAITING constant rather than advancing: intent versions are a
    closed lifecycle vocabulary the door and both workflows compare exactly,
    not a counter, and reopening the door means restoring the exact coordinate
    it accepts.
    """

    revoked = connection.execute(
        reconcile_commands.update()
        .where(
            reconcile_commands.c.command_id == owner_command_id,
            reconcile_commands.c.state == ReconcileCommandState.PENDING.value,
        )
        .values(state=ReconcileCommandState.REJECTED_CONFLICT.value)
    )
    reopened = connection.execute(
        effect_intents.update()
        .where(
            effect_intents.c.logical_key == logical_key.value,
            effect_intents.c.state == EffectIntentState.RECONCILING.value,
            effect_intents.c.state_version == EFFECT_INTENT_VERSION_RECONCILING.value,
            effect_intents.c.reconciliation_owner_command_id == owner_command_id,
        )
        .values(
            state=EffectIntentState.WAITING_RECONCILIATION.value,
            state_version=EFFECT_INTENT_VERSION_WAITING.value,
            reconciliation_owner_command_id=None,
        )
    )
    if revoked.rowcount != 1 or reopened.rowcount != 1:
        raise DurableEffectConflict(
            "a reconciling intent must own exactly one pending command"
        )
