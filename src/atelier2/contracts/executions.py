from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevisionHash,
    require_exact_round_ordinal,
)

if TYPE_CHECKING:
    from atelier2.contracts.agent_attempts import (
        AgentAttemptCancellationDisposition,
        AgentAttemptId,
        AgentAttemptReplacement,
    )
    from atelier2.contracts.agents import AgentExecutionRequestV2
else:
    # agent_attempts owns these exact types but already imports NodeExecutionId.
    # Runtime lower bounds keep recursive contract inspection resolvable without
    # creating that import cycle; each binding constructor enforces the exact type.
    AgentAttemptCancellationDisposition = StrEnum
    AgentAttemptId = Sha256Hash
    AgentAttemptReplacement = StrEnum


class NodeExecutionId(Sha256Hash):
    """Which execution of which node of which run, as one exact identity.

    The round is the fourth dimension, and it is carried by a *nested* family
    rather than by widening the landed one. `node-execution-id/v1` binds run,
    revision and node, and every identity this engine has already written is
    that preimage; a round dimension folded into it would rename every stored
    execution, receipt and attempt at once. So round one is byte-for-byte the
    landed derivation, and a later round frames it again under its own domain
    with the ordinal that distinguishes it. The three-schema discipline of
    `adapters/dbos/workflow_ids.py` is the same rule seen from the other side.
    """

    @classmethod
    def for_node(
        cls,
        run_id: RunId,
        revision_hash: WorkflowRevisionHash,
        node_id: str,
        round_ordinal: int = FIRST_ROUND_ORDINAL,
    ) -> NodeExecutionId:
        require_exact_round_ordinal(round_ordinal)
        first = cls.of(
            frame(
                "node-execution-id/v1",
                run_id.value.encode("utf-8"),
                revision_hash.value.encode("ascii"),
                node_id.encode("utf-8"),
            )
        )
        if round_ordinal == FIRST_ROUND_ORDINAL:
            return first
        return cls.of(
            frame(
                "node-execution-id/round/v1",
                first.value.encode("ascii"),
                str(round_ordinal).encode("ascii"),
            )
        )


@dataclass(frozen=True)
class AgentAttemptExecution:
    request: AgentExecutionRequestV2
    attempt_id: AgentAttemptId
    ordinal: int

    def __post_init__(self) -> None:
        from atelier2.contracts.agent_attempts import AgentAttemptId
        from atelier2.contracts.agents import AgentExecutionRequestV2

        if not isinstance(self.request, AgentExecutionRequestV2):
            raise TypeError("agent attempt execution request must be typed")
        expected = AgentAttemptId.for_execution(
            self.request.node_execution_id, self.request.request_hash, self.ordinal
        )
        if self.attempt_id != expected:
            raise ValueError("agent attempt execution differs from its exact identity")


class RunEventKind(StrEnum):
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_CANCEL_REQUESTED = "AGENT_CANCEL_REQUESTED"
    AGENT_CANCELLED = "AGENT_CANCELLED"
    AGENT_INTERRUPTED = "AGENT_INTERRUPTED"
    ACTION_RECONCILIATION_REQUIRED = "ACTION_RECONCILIATION_REQUIRED"
    ACTION_RECONCILIATION_RESOLVED = "ACTION_RECONCILIATION_RESOLVED"
    ACTION_COMPLETED = "ACTION_COMPLETED"
    WAITING_INPUT = "WAITING_INPUT"
    WAIT_ANSWERED = "WAIT_ANSWERED"
    WAIT_CANCELLED = "WAIT_CANCELLED"
    SUBWORKFLOW_COMPLETED = "SUBWORKFLOW_COMPLETED"


class AgentExecutionRefusal(StrEnum):
    """The product-level reason an Agent node could not start.

    Every word here ends its node before an attempt exists, so a run carrying
    one never launched a provider, never leased a workspace and never pushed.
    The claim words are the lane precondition's own endings: a node that may
    change the project holds its work item's claim first, and each way that can
    fail is named rather than folded into one refusal an operator would have to
    read a log to understand.
    """

    EXECUTOR_BINDING_UNAVAILABLE = "agent-executor-binding-unavailable"
    WORK_ITEM_CLAIM_UNCONFIGURED = "work-item-claim-unconfigured"
    WORK_ITEM_NAMES_NO_SCOPE = "work-item-names-no-scope"
    WORK_ITEM_CLAIM_REFUSED_BY_PRIORITY = "work-item-claim-refused-by-priority"
    WORK_ITEM_CLAIM_LEDGER_UNREADABLE = "work-item-claim-ledger-unreadable"
    WORK_ITEM_CLAIM_REFUSED = "work-item-claim-refused"
    WORK_ITEM_CLAIM_TOUCHES_ANOTHER_LANE = "work-item-claim-touches-another-lane"


_REFUSAL_DETAIL_SEPARATOR = b"\n"


@dataclass(frozen=True)
class AgentNodeRefusalRecord:
    """What an `AGENT_FAILED` payload says about a node that never started.

    `refusal` is the closed word; `detail` is the sentence the refusing boundary
    gave for it -- a claim ledger's own `ERROR:` line -- and empty where it gave
    none. One owner for both directions of the bytes, because a failure event's
    payload is either this record or an attempt's own failure code, and two
    readers that split it differently would answer a run differently in two
    places. A record without detail is the bare word.
    """

    refusal: AgentExecutionRefusal
    detail: str = ""

    def encode(self) -> bytes:
        word = self.refusal.value.encode("ascii")
        if not self.detail:
            return word
        return word + _REFUSAL_DETAIL_SEPARATOR + self.detail.encode("utf-8")

    @classmethod
    def decode(cls, payload: bytes) -> AgentNodeRefusalRecord | None:
        """The record these exact event-payload bytes carry, if they carry one."""

        word, _separator, detail = payload.partition(_REFUSAL_DETAIL_SEPARATOR)
        try:
            refusal = AgentExecutionRefusal(word.decode("ascii"))
        except ValueError:
            return None
        return cls(refusal, detail.decode("utf-8"))

    def sentence(self) -> str:
        """`<word>: <detail>`, or the word alone where nothing more was said."""

        if not self.detail:
            return self.refusal.value
        return f"{self.refusal.value}: {self.detail}"


class WaitAnswerState(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"


class WaitAnswerActor(StrEnum):
    """Who supplied one durable answer, in the current closed vocabulary."""

    OPERATOR = "operator"


class LegacyWaitAnswerAttribution(StrEnum):
    """A predecessor answer whose actor was never durably recorded."""

    UNATTRIBUTED = "legacy-unattributed"


class WaitAnswerAttributionKind(StrEnum):
    """How an answer row can truthfully account for its actor."""

    RECORDED = "RECORDED"
    LEGACY_UNATTRIBUTED = "LEGACY_UNATTRIBUTED"


type WaitAnswerAttribution = WaitAnswerActor | LegacyWaitAnswerAttribution


@dataclass(frozen=True)
class RunEventAgentAttemptBinding:
    attempt_id: AgentAttemptId
    attempt_ordinal: int

    def __post_init__(self) -> None:
        from atelier2.contracts.agent_attempts import AgentAttemptId

        if not isinstance(self.attempt_id, AgentAttemptId):
            raise TypeError("run event attempt id must be typed")
        if type(self.attempt_ordinal) is not int or self.attempt_ordinal not in (1, 2):
            raise ValueError("run event attempt ordinal must be exactly 1 or 2")


@dataclass(frozen=True)
class RunEventCancellationBinding:
    attempt_id: AgentAttemptId
    attempt_ordinal: int
    replacement: AgentAttemptReplacement
    command_id: str
    disposition: AgentAttemptCancellationDisposition | None = None
    replacement_attempt_id: AgentAttemptId | None = None

    def __post_init__(self) -> None:
        from atelier2.contracts.agent_attempts import (
            AgentAttemptCancellationDisposition,
            AgentAttemptId,
            AgentAttemptReplacement,
        )
        from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS

        RunEventAgentAttemptBinding(self.attempt_id, self.attempt_ordinal)
        if not isinstance(self.replacement, AgentAttemptReplacement):
            raise TypeError("run event cancellation replacement policy must be typed")
        if (
            not isinstance(self.command_id, str)
            or not 1 <= len(self.command_id) <= MAXIMUM_AGENT_FIELD_CHARACTERS
        ):
            raise ValueError(
                "run event cancellation command id must contain "
                f"1..{MAXIMUM_AGENT_FIELD_CHARACTERS} characters"
            )
        if self.disposition is not None and not isinstance(
            self.disposition, AgentAttemptCancellationDisposition
        ):
            raise TypeError("run event cancellation disposition must be typed")
        if self.replacement_attempt_id is not None and not isinstance(
            self.replacement_attempt_id, AgentAttemptId
        ):
            raise TypeError("run event replacement attempt id must be typed")
        if self.replacement_attempt_id is not None and self.disposition is None:
            raise ValueError(
                "run event replacement attempt requires a terminal disposition"
            )


type RunEventAttemptBinding = RunEventAgentAttemptBinding | RunEventCancellationBinding


_NO_ATTEMPT_HASH_FIELDS: tuple[bytes, bytes, bytes, bytes, bytes, bytes] = (
    b"",
    b"",
    b"",
    b"",
    b"",
    b"",
)


def _agent_attempt_binding_hash_fields(
    binding: RunEventAgentAttemptBinding,
) -> tuple[bytes, bytes, bytes, bytes, bytes, bytes]:
    """An agent-attempt binding's own event-hash dimensions; it has no
    cancellation fields to carry."""

    return (
        binding.attempt_id.value.encode("ascii"),
        str(binding.attempt_ordinal).encode("ascii"),  # persisted event-hash family
        b"",
        b"",
        b"",
        b"",
    )


def _cancellation_binding_hash_fields(
    binding: RunEventCancellationBinding,
) -> tuple[bytes, bytes, bytes, bytes, bytes, bytes]:
    """A cancellation binding's event-hash dimensions, disposition and
    replacement attempt included where the cancellation has settled them."""

    disposition_field = (
        b""
        if binding.disposition is None
        else binding.disposition.value.encode("ascii")
    )
    replacement_attempt_id_field = (
        b""
        if binding.replacement_attempt_id is None
        else binding.replacement_attempt_id.value.encode("ascii")
    )
    return (
        binding.attempt_id.value.encode("ascii"),
        str(binding.attempt_ordinal).encode("ascii"),  # persisted event-hash family
        binding.command_id.encode("utf-8"),
        binding.replacement.value.encode("ascii"),
        disposition_field,
        replacement_attempt_id_field,
    )


def _attempt_hash_fields(
    attempt_binding: RunEventAttemptBinding | None,
    *,
    use_v2_hash: bool,
    agent_receipt_bound: bool,
) -> tuple[bytes, ...]:
    """The event-hash family's attempt dimensions, present only where a v2+
    hash carries them.

    The family stays nested: v3 carries v2's attempt dimensions unchanged, so
    a completion that binds its receipt does not silently drop the attempt
    binding an ordinal-2 completion already has. Which fields those are
    depends only on the binding's own case, so each case owns its fixed
    six-field shape and this only selects between them.
    """

    if not (use_v2_hash or agent_receipt_bound):
        return ()
    if isinstance(attempt_binding, RunEventCancellationBinding):
        return _cancellation_binding_hash_fields(attempt_binding)
    if isinstance(attempt_binding, RunEventAgentAttemptBinding):
        return _agent_attempt_binding_hash_fields(attempt_binding)
    return _NO_ATTEMPT_HASH_FIELDS


def _require_canonical_event_identity(event: RunEvent) -> None:
    if type(event.event_sequence) is not int or event.event_sequence <= 0:
        raise ValueError("event sequence must be an exact positive integer")
    if event.node_id == "":
        raise ValueError("event node id must be nonempty")
    require_exact_round_ordinal(event.round_ordinal)
    # The round is not a second author of the identity, it is the fourth
    # dimension of the one identity -- so the pair is checked here rather
    # than trusted, exactly as the node id already is. The event hash needs
    # no new domain for it: the execution id it already binds carries it.
    expected_execution = NodeExecutionId.for_node(
        event.run_id, event.revision_hash, event.node_id, event.round_ordinal
    )
    if event.node_execution_id != expected_execution:
        raise ValueError("event execution identity differs from its node binding")


def _require_consistent_wait_actor(event: RunEvent) -> None:
    actor_required = event.event_kind is RunEventKind.WAITING_INPUT
    if actor_required != (event.wait_answer_actor is not None):
        raise ValueError("waiting event and expected answer actor disagree")
    if event.wait_answer_actor is not None and not isinstance(
        event.wait_answer_actor, WaitAnswerActor
    ):
        raise TypeError("waiting event answer actor must be typed")


def _require_consistent_receipt_fields(
    event: RunEvent, payload_hash: Sha256Hash
) -> None:
    receipt_event = event.event_kind in {
        RunEventKind.ACTION_RECONCILIATION_RESOLVED,
        RunEventKind.ACTION_COMPLETED,
    }
    if receipt_event:
        if event.receipt_logical_key is None or event.receipt_result_hash is None:
            raise ValueError("receipt event requires both exact receipt fields")
        if event.receipt_result_hash != payload_hash:
            raise ValueError("receipt event payload must match its receipt result hash")
    elif event.receipt_logical_key is not None or event.receipt_result_hash is not None:
        raise ValueError("nonreceipt event may not carry receipt fields")


def _require_bounded_wait_cancellation_payload(event: RunEvent) -> None:
    # A resting pause has no attempt to stamp, so this event *is* the
    # cancellation's whole attestation and its payload is the operator command
    # id that ordered it -- the only durable trace a retry of that same command
    # can be answered from. Bounded exactly like the `command_id` an attempt
    # cancellation carries in its own column.
    if event.event_kind is not RunEventKind.WAIT_CANCELLED:
        return
    from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS

    if not 1 <= len(event.payload) <= MAXIMUM_AGENT_FIELD_CHARACTERS:
        raise ValueError(
            "a wait cancellation payload must be a command id of "
            f"1..{MAXIMUM_AGENT_FIELD_CHARACTERS} bytes"
        )


def _require_receipt_hash_scope(event: RunEvent) -> None:
    if (
        event.agent_receipt_hash is not None
        and event.event_kind is not RunEventKind.AGENT_COMPLETED
    ):
        raise ValueError("nonagent-completion event may not carry a receipt hash")


def _require_typed_attempt_binding(event: RunEvent) -> None:
    attempt_binding = event.attempt_binding
    if attempt_binding is not None and not isinstance(
        attempt_binding, RunEventAgentAttemptBinding | RunEventCancellationBinding
    ):
        raise TypeError("run event attempt binding must use its closed type")


def _require_canonical_attempt_binding_shape(event: RunEvent) -> None:
    attempt_binding = event.attempt_binding
    if event.event_kind in {
        RunEventKind.AGENT_CANCEL_REQUESTED,
        RunEventKind.AGENT_CANCELLED,
        RunEventKind.AGENT_INTERRUPTED,
    }:
        if not isinstance(attempt_binding, RunEventCancellationBinding):
            raise ValueError("cancellation event requires its exact command binding")
        terminal_cancellation = event.event_kind in {
            RunEventKind.AGENT_CANCELLED,
            RunEventKind.AGENT_INTERRUPTED,
        }
        if terminal_cancellation != (attempt_binding.disposition is not None):
            raise ValueError("cancellation event disposition shape disagrees")
        if not terminal_cancellation and attempt_binding.replacement_attempt_id:
            raise ValueError("cancellation request may not name a replacement attempt")
    elif isinstance(attempt_binding, RunEventCancellationBinding):
        raise ValueError("noncancellation event may not carry a cancellation binding")
    elif attempt_binding is not None and event.event_kind not in {
        RunEventKind.AGENT_COMPLETED,
        RunEventKind.AGENT_FAILED,
    }:
        raise ValueError("nonagent event may not carry an attempt binding")


def _event_hash_domain(*, agent_receipt_bound: bool, use_v2_hash: bool) -> str:
    """Which structural hash domain an event's own shape selects."""

    if agent_receipt_bound:
        return "node-event-hash/v3"
    if use_v2_hash:
        return "node-event-hash/v2"
    return "node-event-hash/v1"


def _event_hash(event: RunEvent, payload_hash: Sha256Hash) -> Sha256Hash:
    """The durable event hash, whose exact field family follows the event's shape."""

    attempt_binding = event.attempt_binding
    agent_receipt_bound = event.agent_receipt_hash is not None
    use_v2_hash = event.event_kind in {
        RunEventKind.AGENT_CANCEL_REQUESTED,
        RunEventKind.AGENT_CANCELLED,
        RunEventKind.AGENT_INTERRUPTED,
    } or (attempt_binding is not None and attempt_binding.attempt_ordinal == 2)
    hash_domain = _event_hash_domain(
        agent_receipt_bound=agent_receipt_bound, use_v2_hash=use_v2_hash
    )
    attempt_fields = _attempt_hash_fields(
        attempt_binding,
        use_v2_hash=use_v2_hash,
        agent_receipt_bound=agent_receipt_bound,
    )
    receipt_fields = (
        ()
        if event.agent_receipt_hash is None
        else (event.agent_receipt_hash.value.encode("ascii"),)
    )
    return Sha256Hash.of(
        frame(
            hash_domain,
            event.run_id.value.encode("utf-8"),
            event.revision_hash.value.encode("ascii"),
            str(event.event_sequence).encode("ascii"),  # persisted event-hash family
            event.node_execution_id.value.encode("ascii"),
            event.event_kind.value.encode("ascii"),
            event.payload,
            payload_hash.value.encode("ascii"),
            (
                b""
                if event.receipt_logical_key is None
                else event.receipt_logical_key.value.encode("utf-8")
            ),
            (
                b""
                if event.receipt_result_hash is None
                else event.receipt_result_hash.value.encode("ascii")
            ),
            *attempt_fields,
            *receipt_fields,
        )
    )


@dataclass(frozen=True)
class RunEvent:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    event_sequence: int
    node_id: str
    node_execution_id: NodeExecutionId
    event_kind: RunEventKind
    payload: bytes
    payload_hash: Sha256Hash = field(init=False)
    receipt_logical_key: LogicalEffectKey | None = None
    receipt_result_hash: Sha256Hash | None = None
    attempt_binding: RunEventAttemptBinding | None = None
    agent_receipt_hash: Sha256Hash | None = None
    round_ordinal: int = FIRST_ROUND_ORDINAL
    wait_answer_actor: WaitAnswerActor | None = None
    event_hash: Sha256Hash = field(init=False)

    def __post_init__(self) -> None:
        _require_canonical_event_identity(self)
        _require_consistent_wait_actor(self)
        payload_hash = Sha256Hash.of(self.payload)
        object.__setattr__(self, "payload_hash", payload_hash)
        _require_consistent_receipt_fields(self, payload_hash)
        _require_bounded_wait_cancellation_payload(self)
        _require_receipt_hash_scope(self)
        _require_typed_attempt_binding(self)
        _require_canonical_attempt_binding_shape(self)
        object.__setattr__(self, "event_hash", _event_hash(self, payload_hash))


@dataclass(frozen=True)
class TransitionSnapshot:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    current_node_id: str
    state: RunState
    state_version: int
    last_event_sequence: int
    event: RunEvent
    current_round_ordinal: int = FIRST_ROUND_ORDINAL


@dataclass(frozen=True)
class WaitAnswer:
    """The bytes one person gave to one exact execution of one waiting node.

    The round is what makes it exact. A node a declared loop turns pauses once
    per round, and an answer that named only the node would let a message typed
    for round two be applied to round three -- so the round is part of the
    identity the execution id is checked against, never a label beside it.
    """

    run_id: RunId
    revision_hash: WorkflowRevisionHash
    node_id: str
    node_execution_id: NodeExecutionId
    actor: WaitAnswerAttribution
    answer_bytes: bytes
    round_ordinal: int = FIRST_ROUND_ORDINAL
    answer_hash: Sha256Hash = field(init=False)

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("answer node id must be nonempty")
        if not isinstance(self.actor, WaitAnswerActor | LegacyWaitAnswerAttribution):
            raise TypeError("answer attribution must use its closed type")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.revision_hash, self.node_id, self.round_ordinal
        ):
            raise ValueError("answer execution identity differs from its node binding")
        object.__setattr__(self, "answer_hash", Sha256Hash.of(self.answer_bytes))


@dataclass(frozen=True)
class WaitAnswerSnapshot:
    answer: WaitAnswer
    state: WaitAnswerState
    state_version: int


@dataclass(frozen=True)
class SubmitWaitAnswerRequest:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    node_id: str
    expected_node_execution_id: NodeExecutionId
    actor: WaitAnswerActor
    answer_bytes: bytes

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("answer node id must be nonempty")
        if not isinstance(self.expected_node_execution_id, NodeExecutionId):
            raise TypeError("answer execution fence must be typed")
        if not isinstance(self.actor, WaitAnswerActor):
            raise TypeError("answer actor must be typed")


def logical_effect_key_for(execution_id: NodeExecutionId) -> LogicalEffectKey:
    digest = Sha256Hash.of(
        frame("logical-effect-key/v1", execution_id.value.encode("ascii"))
    )
    return LogicalEffectKey(f"atelier2-node-effect-{digest.value}")


def work_item_claim_effect_key(execution_id: NodeExecutionId) -> LogicalEffectKey:
    """The key of the claim one node execution takes before it works.

    Framed apart from `logical_effect_key_for` because the same execution also
    prepares the effect its own grant earns: two effects of one node execution
    are two keys, or the second would find the first's intent and call it its
    own. Derived from the execution alone, so a reader that holds only that --
    the restart sweep asking who owns an open intent -- computes it too.
    """
    digest = Sha256Hash.of(
        frame(
            "logical-effect-key/work-item-claim/v1",
            execution_id.value.encode("ascii"),
        )
    )
    return LogicalEffectKey(f"atelier2-work-item-claim-{digest.value}")


def logical_effect_key_for_work_item_claim(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> LogicalEffectKey:
    """That same key, from the four coordinates a preparer holds."""
    return work_item_claim_effect_key(
        NodeExecutionId.for_node(run_id, revision_hash, node_id, round_ordinal)
    )


def logical_effect_key_for_node(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> LogicalEffectKey:
    """The logical effect key of one node's exact, round-bound execution.

    Every reader that must agree a key belongs to *this* execution -- the
    preparer that mints it, the completer that checks it still owns the run's
    current node, and the convergence sweep that routes a stranded one home --
    derives it from the same four coordinates: run, revision, node and round.
    A caller that reconstructs `NodeExecutionId.for_node` by hand and forgets
    the round silently reuses round one's key for every later round, and a
    round-aware reader then finds a key that owns nothing it can match. This is
    the one owner of that composition, so the mistake cannot be made a second
    time in a fourth call site (#706).
    """
    return logical_effect_key_for(
        NodeExecutionId.for_node(run_id, revision_hash, node_id, round_ordinal)
    )


def terminal_hash_for(
    revision_hash: WorkflowRevisionHash, event_hashes: tuple[Sha256Hash, ...]
) -> Sha256Hash:
    return Sha256Hash.of(
        frame(
            "run-terminal-hash/v1",
            revision_hash.value.encode("ascii"),
            *(event_hash.value.encode("ascii") for event_hash in event_hashes),
        )
    )
