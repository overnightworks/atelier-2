"""Projection of a persisted run event onto the wire schema this API serves."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Literal, TypedDict, cast
from urllib.parse import quote

from atelier2.api.projection.workflows import (
    UnservedWorkflowFormat,
    receipt_resource,
)
from atelier2.api.references import (
    encode_canonical_base64,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.api.wire.events import (
    ActionCompletedEventResourceV3,
    ActionReconciliationRequiredEventResourceV3,
    ActionReconciliationResolvedEventResourceV3,
    AgentCancelledEventResourceV3,
    AgentCancelRequestedEventResourceV3,
    AgentCompletedEventResourceV3,
    AgentExecutorBindingUnavailableEventResourceV3,
    AgentFailedEventResourceV3,
    AgentInterruptedEventResourceV3,
    RunEventResourceV3,
    WaitAnsweredEventResourceV3,
    WaitCancelledEventResourceV3,
    WaitingInputEventResourceV3,
)
from atelier2.api.wire.resources import (
    AgentAttemptFailureCodeName,
    AgentNodeRefusalName,
    CancellationDispositionName,
    NodeRailResource,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    RunEvent,
    RunEventCancellationBinding,
    RunEventKind,
)
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.workflow_formats import WorkflowFormatVersion

_RECEIPT_REASON_OMITTED = "Receipt reason omitted because it exceeds the event limit."
_NODE_DETAIL_FALLBACK = (
    " Its node-detail resource is identified by this event's public_run_reference and "
    "node_id fields."
)


def _omitted_receipt_reason_summary(projection: PersistedRunEvent) -> str:
    """Name the exact detail door when its identifiers fit the event field."""

    event = projection.event
    public_run_reference = encode_public_run_reference(event.run_id)
    encoded_node_id = quote(event.node_id, safe="-_.!~*'()")
    summary = (
        f"{_RECEIPT_REASON_OMITTED} Read the node-detail resource at GET "
        f"/atelier/api/v1/runs/{public_run_reference}/nodes/{encoded_node_id} for the "
        "full reason and refusal output."
    )
    if len(summary) <= MAXIMUM_AGENT_FIELD_CHARACTERS:
        return summary
    return _RECEIPT_REASON_OMITTED + _NODE_DETAIL_FALLBACK


def bounded_event_summary(projection: PersistedRunEvent) -> PersistedRunEvent:
    """Keep an event readable when its durable receipt reason is over the wire bound."""

    reason = projection.node_receipt_reason
    if reason is None or len(reason) <= MAXIMUM_AGENT_FIELD_CHARACTERS:
        return projection
    return replace(
        projection, node_receipt_reason=_omitted_receipt_reason_summary(projection)
    )


class _CommonEventFields(TypedDict):
    """What every format-3 event resource carries before its kind adds the rest."""

    workflow_format_version: Literal[3]
    node_rail: tuple[NodeRailResource, ...]
    cursor: str
    sequence: int
    public_run_reference: str
    workflow_revision_hash: str
    node_id: str
    node_execution_id: str
    event_hash: str


def run_event_resource(
    projection: PersistedRunEvent, node_rail: tuple[NodeRailResource, ...]
) -> RunEventResourceV3:
    """One durable event on the wire, carrying where its run stands after it.

    Every run this API serves is format 3, so an event that names another format
    is durable state the wire has no shape for and is refused rather than bent
    into one.
    """

    if projection.workflow_format_version is not WorkflowFormatVersion.V3:
        raise UnservedWorkflowFormat(
            "the API projects format-3 events only, not format "
            f"{projection.workflow_format_version.value}"
        )
    projection = bounded_event_summary(projection)
    event = projection.event
    common = _CommonEventFields(
        workflow_format_version=3,
        node_rail=node_rail,
        cursor=encode_event_cursor(event.run_id, event.event_sequence),
        sequence=event.event_sequence,
        public_run_reference=encode_public_run_reference(event.run_id),
        workflow_revision_hash=event.revision_hash.value,
        node_id=event.node_id,
        node_execution_id=event.node_execution_id.value,
        event_hash=event.event_hash.value,
    )
    resource_of = _EVENT_RESOURCES.get(event.event_kind)
    if resource_of is None:
        raise ValueError(f"a V3 run cannot carry {event.event_kind.value}")
    return resource_of(projection, common)


def _agent_completed(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> AgentCompletedEventResourceV3:
    event = projection.event
    binding = event.attempt_binding
    if binding is None:
        raise ValueError("V3 agent completion has no exact attempt binding")
    return AgentCompletedEventResourceV3(
        event="AGENT_COMPLETED",
        output_base64=encode_canonical_base64(event.payload),
        output_hash=event.payload_hash.value,
        attempt_id=binding.attempt_id.value,
        attempt_ordinal=cast(Literal[1, 2], binding.attempt_ordinal),
        **common,
    )


def _agent_failed(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> AgentExecutorBindingUnavailableEventResourceV3 | AgentFailedEventResourceV3:
    event = projection.event
    refusal = AgentExecutionRefusal.named_by(event.payload)
    if refusal is not None:
        if event.attempt_binding is not None:
            raise ValueError("a pre-attempt refusal event has an attempt binding")
        return AgentExecutorBindingUnavailableEventResourceV3(
            event="AGENT_FAILED",
            reason=cast(AgentNodeRefusalName, refusal.value),
            **common,
        )
    failure_code = event.payload.decode("ascii")
    if failure_code not in {code.value for code in AgentAttemptFailureCode}:
        raise ValueError("durable agent failure payload is not canonical")
    binding = event.attempt_binding
    if binding is None:
        raise ValueError("V3 agent failure has no exact attempt binding")
    return AgentFailedEventResourceV3(
        event="AGENT_FAILED",
        failure_code=cast(AgentAttemptFailureCodeName, failure_code),
        reason=projection.node_receipt_reason,
        attempt_id=binding.attempt_id.value,
        attempt_ordinal=cast(Literal[1, 2], binding.attempt_ordinal),
        **common,
    )


def _cancellation_binding(event: RunEvent, phase: str) -> RunEventCancellationBinding:
    binding = event.attempt_binding
    if isinstance(binding, RunEventCancellationBinding):
        return binding
    raise ValueError(f"V3 cancellation {phase} has no exact binding")


def _agent_cancel_requested(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> AgentCancelRequestedEventResourceV3:
    event = projection.event
    binding = _cancellation_binding(event, "request")
    return AgentCancelRequestedEventResourceV3(
        event="AGENT_CANCEL_REQUESTED",
        attempt_id=binding.attempt_id.value,
        attempt_ordinal=cast(Literal[1, 2], binding.attempt_ordinal),
        command_id=binding.command_id,
        replacement=cast(Literal["NONE", "ONE"], binding.replacement.value),
        **common,
    )


def _cancellation_terminal(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> AgentCancelledEventResourceV3 | AgentInterruptedEventResourceV3:
    event = projection.event
    binding = _cancellation_binding(event, "terminal")
    if binding.disposition is None:
        raise ValueError("V3 cancellation terminal has no exact binding")
    terminal_common = {
        "attempt_id": binding.attempt_id.value,
        "attempt_ordinal": cast(Literal[1, 2], binding.attempt_ordinal),
        "command_id": binding.command_id,
        "replacement": cast(Literal["NONE", "ONE"], binding.replacement.value),
        "disposition": cast(
            CancellationDispositionName,
            binding.disposition.value,
        ),
        "replacement_attempt_id": (
            None
            if binding.replacement_attempt_id is None
            else binding.replacement_attempt_id.value
        ),
    }
    if event.event_kind is RunEventKind.AGENT_CANCELLED:
        return AgentCancelledEventResourceV3(
            event=event.event_kind.value, **terminal_common, **common
        )
    return AgentInterruptedEventResourceV3(
        event="AGENT_INTERRUPTED", **terminal_common, **common
    )


def _action_reconciliation_required(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> ActionReconciliationRequiredEventResourceV3:
    event = projection.event
    return ActionReconciliationRequiredEventResourceV3(
        event="ACTION_RECONCILIATION_REQUIRED",
        request_base64=encode_canonical_base64(event.payload),
        request_hash=event.payload_hash.value,
        **common,
    )


def _action_reconciliation_resolved(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> ActionReconciliationResolvedEventResourceV3:
    if projection.receipt is None:
        raise ValueError("resolved V3 event has no receipt")
    return ActionReconciliationResolvedEventResourceV3(
        event="ACTION_RECONCILIATION_RESOLVED",
        receipt=receipt_resource(projection.receipt),
        **common,
    )


def _action_completed(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> ActionCompletedEventResourceV3:
    if projection.receipt is None:
        raise ValueError("completed V3 effect event has no receipt")
    return ActionCompletedEventResourceV3(
        event="ACTION_COMPLETED",
        receipt=receipt_resource(projection.receipt),
        **common,
    )


def _waiting_input(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> WaitingInputEventResourceV3:
    return WaitingInputEventResourceV3(event="WAITING_INPUT", **common)


def _wait_answered(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> WaitAnsweredEventResourceV3:
    if projection.wait_answer_actor is None:
        raise ValueError("V3 wait answer event has no actor attribution")
    event = projection.event
    return WaitAnsweredEventResourceV3(
        event="WAIT_ANSWERED",
        actor=projection.wait_answer_actor.value,
        answer_base64=encode_canonical_base64(event.payload),
        answer_hash=event.payload_hash.value,
        **common,
    )


def _wait_cancelled(
    projection: PersistedRunEvent, common: _CommonEventFields
) -> WaitCancelledEventResourceV3:
    event = projection.event
    return WaitCancelledEventResourceV3(
        event="WAIT_CANCELLED",
        command_id=event.payload.decode("utf-8"),
        **common,
    )


_EVENT_RESOURCES: dict[
    RunEventKind, Callable[[PersistedRunEvent, _CommonEventFields], RunEventResourceV3]
] = {
    RunEventKind.AGENT_COMPLETED: _agent_completed,
    RunEventKind.AGENT_FAILED: _agent_failed,
    RunEventKind.AGENT_CANCEL_REQUESTED: _agent_cancel_requested,
    RunEventKind.AGENT_CANCELLED: _cancellation_terminal,
    RunEventKind.AGENT_INTERRUPTED: _cancellation_terminal,
    RunEventKind.ACTION_RECONCILIATION_REQUIRED: _action_reconciliation_required,
    RunEventKind.ACTION_RECONCILIATION_RESOLVED: _action_reconciliation_resolved,
    RunEventKind.ACTION_COMPLETED: _action_completed,
    RunEventKind.WAITING_INPUT: _waiting_input,
    RunEventKind.WAIT_ANSWERED: _wait_answered,
    RunEventKind.WAIT_CANCELLED: _wait_cancelled,
}
"""Which kinds a format-3 run can carry, and the shape each becomes on the wire."""
