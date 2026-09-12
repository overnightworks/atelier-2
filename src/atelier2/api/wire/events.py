"""The schemas an event stream frames for the runs this API serves."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    MAX_SIGNED_INT64,
    PUBLIC_RUN_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.api.wire.resources import (
    AgentAttemptFailureCodeName,
    AgentNodeRefusalName,
    ApiModel,
    CancellationDispositionName,
    EffectReceiptResource,
    NodeRailResource,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS


class RunEventBaseResourceV3(ApiModel):
    workflow_format_version: Literal[3]
    cursor: str = Field(pattern=EVENT_CURSOR_PATTERN)
    sequence: int = Field(ge=1, le=MAX_SIGNED_INT64)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    event_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    """Where the whole run stands once this event is folded into the snapshot.

    A reader of one event needs the run, not the node: a finished node hands its
    successor work, and answering that from the event alone is the derivation
    this rail exists to end. Subworkflow resources are not invented here: no
    format-3 run persists that kind today.
    """


class AgentCompletedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_COMPLETED"]
    output_base64: str
    output_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentFailedEventResourceV3(RunEventBaseResourceV3):
    """A V3 agent attempt ended, with the stored receipt words when they exist.

    `failure_code` is the attempt's closed name. `reason` is the same
    `node-receipt/v3` sentence the store already kept — not a second
    vocabulary, and none when no receipt was written.
    """

    event: Literal["AGENT_FAILED"]
    failure_code: AgentAttemptFailureCodeName
    reason: str | None
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentExecutorBindingUnavailableEventResourceV3(RunEventBaseResourceV3):
    """An Agent node stopped before any provider attempt could be claimed.

    `reason` is `AgentExecutionRefusal`'s closed set -- an executor nothing can
    bind, a node whose binding runs outside its declared mode, and the lane
    claim a node must hold before it works -- written out here because a wire
    schema may name no contract enum inline;
    `test_the_wire_refusal_literal_and_the_refusal_enum_cannot_drift` pins the
    two spellings to set equality. `detail` is the sentence the refusing
    boundary gave, the claim ledger's own line, and none where it gave none.
    """

    event: Literal["AGENT_FAILED"]
    reason: AgentNodeRefusalName
    detail: str | None


class AgentCancelRequestedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_CANCEL_REQUESTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]


class AgentCancelledEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_CANCELLED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: CancellationDispositionName
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class AgentInterruptedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_INTERRUPTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: CancellationDispositionName
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class WaitingInputEventResourceV3(RunEventBaseResourceV3):
    """A format-3 run has stopped at a Wait node and is owed an answer.

    It carries no `answer_type`. A Wait node declares an output with a schema, so
    the honest answer to "what may I send" is the published schema that node
    pinned, reachable from the workflow revision this event names -- and stating
    `integer` here would be a shape nothing enforces.
    """

    event: Literal["WAITING_INPUT"]


class ActionReconciliationRequiredEventResourceV3(RunEventBaseResourceV3):
    event: Literal["ACTION_RECONCILIATION_REQUIRED"]
    request_base64: str
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationResolvedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["ACTION_RECONCILIATION_RESOLVED"]
    receipt: EffectReceiptResource


class ActionCompletedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["ACTION_COMPLETED"]
    receipt: EffectReceiptResource


class WaitAnsweredEventResourceV3(RunEventBaseResourceV3):
    """The exact bytes a person answered with, and the hash the run kept.

    Base64 rather than the V2 shape's decimal text, for the same reason an agent
    output travels that way: a V3 answer is whatever JSON value its schema
    admits, and rendering it as a number would be a lie for every other value.
    """

    event: Literal["WAIT_ANSWERED"]
    actor: Literal["operator", "legacy-unattributed"]
    answer_base64: str
    answer_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class WaitCancelledEventResourceV3(RunEventBaseResourceV3):
    """An operator ended this run while it was resting at this pause.

    The event is the cancellation's whole attestation -- a pause has no attempt
    to stamp -- so it names the command that ordered it and nothing else. The
    run is `CANCELLED` from here; no answer to this pause is owed any more.
    """

    event: Literal["WAIT_CANCELLED"]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)


RunEventResourceV3 = (
    AgentCompletedEventResourceV3
    | AgentFailedEventResourceV3
    | AgentExecutorBindingUnavailableEventResourceV3
    | AgentCancelRequestedEventResourceV3
    | AgentCancelledEventResourceV3
    | AgentInterruptedEventResourceV3
    | ActionReconciliationRequiredEventResourceV3
    | ActionReconciliationResolvedEventResourceV3
    | ActionCompletedEventResourceV3
    | WaitingInputEventResourceV3
    | WaitAnsweredEventResourceV3
    | WaitCancelledEventResourceV3
)
