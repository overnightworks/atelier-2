"""A format-3 event keeps its own family, and the V1 pin stays pinned.

The mapper used to treat every non-2 event as V1. That dressed a V3 completion
as utf-8 `output` without format or rail, and sent a V3 failure down the V1
"cannot carry" path. These cases pin the V3 family at that owner.
"""

from __future__ import annotations

from dataclasses import replace
from typing import get_args

import pytest

from atelier2.api.projection.events import run_event_resource
from atelier2.api.references import encode_canonical_base64
from atelier2.api.wire.events import (
    ActionCompletedEventResourceV3,
    AgentCompletedEventResourceV3,
    RunEventResourceV3,
)
from atelier2.api.wire.resources import AgentNodeRefusalName
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode, AgentAttemptId
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectReceipt,
    EffectResult,
    LogicalEffectKey,
)
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventKind,
    WaitAnswerActor,
)
from atelier2.contracts.run_cancellations import RunCancelCommandId
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from tests.api.test_agent_attempts import SERVED_RAIL

RUN_ID = RunId("v3-event-vocabulary")
REVISION_HASH = WorkflowRevisionHash("0" * 64)
NODE_ID = "implement"
ATTEMPT_ID = "a" * 64
COMPLETED_PAYLOAD = b"hello"


def effect_receipt() -> EffectReceipt:
    binding = EffectBinding(
        logical_key=LogicalEffectKey("v3-event-vocabulary/effect"),
        run_id=RUN_ID,
        workflow_revision_hash=REVISION_HASH,
        adapter_revision=AdapterRevision("adapter-1"),
        destination=EffectDestination("destination"),
        adapter_operational_identity=AdapterOperationalIdentity("installation-1"),
    )
    return EffectReceipt(
        intent=EffectIntent(binding, CanonicalRequest(b"request")),
        effect_id=EffectId("effect-1"),
        result=EffectResult(b"result"),
        confirmation_source=ConfirmationSource.ADAPTER_READBACK,
    )


def v3_projection(kind: RunEventKind, payload: bytes) -> PersistedRunEvent:
    attempt_binding = (
        RunEventAgentAttemptBinding(AgentAttemptId(ATTEMPT_ID), 1)
        if kind in {RunEventKind.AGENT_COMPLETED, RunEventKind.AGENT_FAILED}
        else None
    )
    event = RunEvent(
        RUN_ID,
        REVISION_HASH,
        1,
        NODE_ID,
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, NODE_ID),
        kind,
        payload,
        attempt_binding=attempt_binding,
        wait_answer_actor=(
            WaitAnswerActor.OPERATOR if kind is RunEventKind.WAITING_INPUT else None
        ),
    )
    return PersistedRunEvent(
        event,
        None,
        WorkflowFormatVersion.V3,
        wait_answer_actor=(
            WaitAnswerActor.OPERATOR if kind is RunEventKind.WAIT_ANSWERED else None
        ),
    )


def refused_v3_projection(
    refusal: AgentExecutionRefusal = AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE,
) -> PersistedRunEvent:
    event = RunEvent(
        RUN_ID,
        REVISION_HASH,
        1,
        NODE_ID,
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, NODE_ID),
        RunEventKind.AGENT_FAILED,
        refusal.value.encode("ascii"),
    )
    return PersistedRunEvent(event, None, WorkflowFormatVersion.V3)


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_format_3_agent_completed_carries_its_output_as_exact_bytes() -> None:
    projection = v3_projection(RunEventKind.AGENT_COMPLETED, COMPLETED_PAYLOAD)

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["output_base64"] == encode_canonical_base64(COMPLETED_PAYLOAD)
    assert dumped["output_hash"] == projection.event.payload_hash.value
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]
    assert dumped["attempt_id"] == ATTEMPT_ID
    assert dumped["attempt_ordinal"] == 1
    assert isinstance(resource, AgentCompletedEventResourceV3)


@pytest.mark.parametrize("failure_code", tuple(AgentAttemptFailureCode))
@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_format_3_agent_failed_names_its_failure_code_and_its_attempt(
    failure_code: AgentAttemptFailureCode,
) -> None:
    """Every name an attempt can fail under reaches the event a reader sees.

    Driven from the owner's own membership rather than a hand-kept list: a code
    the served vocabulary does not carry is a durable payload the projection
    refuses, which is how `CANDIDATE_CAPTURE_FAILED` once reached nobody (#642).
    """
    resource = run_event_resource(
        v3_projection(RunEventKind.AGENT_FAILED, failure_code.value.encode("ascii")),
        SERVED_RAIL,
    )

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "AGENT_FAILED"
    assert dumped["failure_code"] == failure_code.value
    assert dumped["reason"] is None
    assert dumped["attempt_id"] == ATTEMPT_ID
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]
    assert type(resource).__name__ == "AgentFailedEventResourceV3"


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_format_3_pre_attempt_executor_refusal_has_no_attempt_failure_shape() -> None:
    dumped = run_event_resource(refused_v3_projection(), SERVED_RAIL).model_dump(
        mode="json"
    )

    assert dumped["event"] == "AGENT_FAILED"
    assert dumped["reason"] == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
    assert "failure_code" not in dumped
    assert "attempt_id" not in dumped
    assert "attempt_ordinal" not in dumped


def test_the_wire_refusal_literal_and_the_refusal_enum_cannot_drift() -> None:
    """A node that never started says why in the vocabulary the API serves.

    The wire spells this set out as a Literal, so a refusal the runtime can
    write and the wire cannot name would answer a reader with a 500 instead of
    the reason its run ended on.
    """

    served = {
        run_event_resource(refused_v3_projection(refusal), SERVED_RAIL).model_dump(
            mode="json"
        )["reason"]
        for refusal in AgentExecutionRefusal
    }

    assert served == {refusal.value for refusal in AgentExecutionRefusal}
    assert served == set(get_args(AgentNodeRefusalName))


@pytest.mark.proves("an-agent-failed-event-carries-the-stored-receipt-reason")
def test_a_v3_failure_carries_the_stored_receipt_reason_when_one_was_written() -> None:
    words = "output-schema-refused: instance-not-json: Expecting value"
    projection = replace(
        v3_projection(RunEventKind.AGENT_FAILED, b"OUTPUT_SCHEMA_REFUSED"),
        node_receipt_reason=words,
    )

    dumped = run_event_resource(projection, SERVED_RAIL).model_dump(mode="json")

    assert dumped["failure_code"] == "OUTPUT_SCHEMA_REFUSED"
    assert dumped["reason"] == words


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_a_v3_failure_admits_project_verification_failed() -> None:
    words = "project-verification-failed: exit 1"
    projection = replace(
        v3_projection(RunEventKind.AGENT_FAILED, b"PROJECT_VERIFICATION_FAILED"),
        node_receipt_reason=words,
    )

    dumped = run_event_resource(projection, SERVED_RAIL).model_dump(mode="json")

    assert dumped["failure_code"] == "PROJECT_VERIFICATION_FAILED"
    assert dumped["reason"] == words


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_format_3_waiting_input_says_a_person_is_owed_a_move_and_names_no_type() -> (
    None
):
    """The pause reads back in the V3 family, without the V2 answer vocabulary.

    A V1 or V2 Wait node declares `answer_type: integer` and its event says so. A
    V3 Wait node declares an output with a schema instead, so naming a type here
    would state a shape nothing in this format enforces.
    """
    projection = v3_projection(RunEventKind.WAITING_INPUT, b"")

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "WAITING_INPUT"
    assert "answer_type" not in dumped
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_format_3_wait_answered_carries_the_exact_bytes_a_person_sent() -> None:
    """A V3 answer is whatever its schema admits, so it travels as bytes.

    The V2 shape renders a decimal string, which only its own `integer` wait can
    honestly produce; a JSON string, object or array read through that shape
    would be a value the wire misdescribes.
    """
    answer = b'{"verdict": "approved"}'
    projection = v3_projection(RunEventKind.WAIT_ANSWERED, answer)

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "WAIT_ANSWERED"
    assert dumped["actor"] == WaitAnswerActor.OPERATOR.value
    assert dumped["answer_base64"] == encode_canonical_base64(answer)
    assert dumped["answer_hash"] == projection.event.payload_hash.value
    assert "answer" not in dumped


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_format_3_action_completed_answers_with_the_v2_receipt_and_the_v3_rail() -> (
    None
):
    receipt = effect_receipt()
    event = RunEvent(
        RUN_ID,
        REVISION_HASH,
        1,
        "publish",
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "publish"),
        RunEventKind.ACTION_COMPLETED,
        receipt.result.payload,
        receipt_logical_key=receipt.intent.binding.logical_key,
        receipt_result_hash=receipt.result.payload_hash,
    )
    projection = PersistedRunEvent(event, receipt, WorkflowFormatVersion.V3)

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "ACTION_COMPLETED"
    assert dumped["receipt"]["result_base64"] == encode_canonical_base64(
        receipt.result.payload
    )
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]
    assert isinstance(resource, ActionCompletedEventResourceV3)


def test_format_3_wait_cancelled_names_the_command_that_ended_the_pause() -> None:
    """The whole attestation an operator's cancel of a resting pause leaves.

    It names the command and nothing else: there is no attempt behind it to
    carry an id, an ordinal or a disposition, and inventing any of those would
    describe machinery this event does not have.
    """
    command_id = RunCancelCommandId.for_key("operator-stops-the-wait").value
    projection = v3_projection(RunEventKind.WAIT_CANCELLED, command_id.encode("utf-8"))

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "WAIT_CANCELLED"
    assert dumped["command_id"] == command_id
    assert "attempt_id" not in dumped
    assert "disposition" not in dumped
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]


PUBLISHED_EVENT_NAMES = frozenset(
    get_args(member.model_fields["event"].annotation)[0]
    for member in get_args(RunEventResourceV3)
)
"""The event names the served union publishes, read from the union itself.

Restating them here is what let the projection and the published schema drift
apart in the first place, so the vocabulary has one owner and this file derives
from it.
"""


@pytest.mark.parametrize(
    "kind",
    sorted(kind for kind in RunEventKind if kind.value not in PUBLISHED_EVENT_NAMES),
)
@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_a_kind_the_served_union_does_not_publish_refuses_by_name(
    kind: RunEventKind,
) -> None:
    """An unmapped kind refuses by name instead of leaving the union silently.

    The mapping and the published union are two spellings of one vocabulary. A
    kind that reaches the projection without an arm must say which kind it was,
    because a fall-through would answer some other shape or none at all.
    """

    unpublished = PersistedRunEvent(
        event=RunEvent(
            run_id=RUN_ID,
            revision_hash=REVISION_HASH,
            event_sequence=1,
            node_id=NODE_ID,
            node_execution_id=NodeExecutionId.for_node(RUN_ID, REVISION_HASH, NODE_ID),
            event_kind=kind,
            payload=b"5",
        ),
        workflow_format_version=WorkflowFormatVersion.V3,
        receipt=None,
    )

    with pytest.raises(ValueError, match=f"a V3 run cannot carry {kind.value}"):
        run_event_resource(unpublished, SERVED_RAIL)
