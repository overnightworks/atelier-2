"""The provider-neutral conversation seam: one provider's wire format as a state
machine, the frames it may write and read, and how its process ends.

`atelier2.ports.agent_executions` owns the executor and session surface that
opens, holds, and supervises one of these; this module owns only the vocabulary
a conversation itself speaks, independent of any executor that opens one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from atelier2.contracts.agent_permissions import PermissionDecision, PermissionRequest
from atelier2.contracts.agent_transcripts import TranscriptEvent
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
)


@dataclass(frozen=True, slots=True)
class ProviderConversationBounds:
    """Every buffer one conversation may cost, declared by the executor that opens it.

    The executor knows its provider's wire format and therefore what a frame, a
    reply and a cancellation may cost there. Supervision owns the physical
    buffers and holds each one to exactly this declaration, because a bound the
    side that allocates does not enforce is a wish. Every bound is refused
    against the port's own portable ceilings here, so no executor can declare
    itself room the process seam could never give it.
    """

    maximum_total_output_bytes: int
    maximum_incomplete_frame_bytes: int
    maximum_reply_bytes: int
    maximum_cancel_bytes: int
    maximum_pending_input_bytes: int

    def __post_init__(self) -> None:
        declared = (
            self.maximum_total_output_bytes,
            self.maximum_incomplete_frame_bytes,
            self.maximum_reply_bytes,
            self.maximum_cancel_bytes,
            self.maximum_pending_input_bytes,
        )
        if any(type(bound) is not int or bound < 1 for bound in declared):
            raise ValueError("every conversation bound counts at least one byte")
        if (
            self.maximum_total_output_bytes
            > MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES
            or self.maximum_incomplete_frame_bytes > self.maximum_total_output_bytes
        ):
            raise ValueError(
                "a conversation cannot read past the portable output bound"
            )
        if (
            self.maximum_pending_input_bytes > MAXIMUM_AGENT_PROCESS_INPUT_BYTES
            or self.maximum_reply_bytes > self.maximum_pending_input_bytes
            or self.maximum_cancel_bytes > self.maximum_pending_input_bytes
        ):
            raise ValueError(
                "a conversation cannot write past the portable input bound"
            )


@dataclass(frozen=True, slots=True)
class ProviderStandardInput:
    """Bytes this conversation wants written to its child's standard input."""

    data: bytes


@dataclass(frozen=True, slots=True)
class ProviderCancellationFrame:
    """How this provider is asked to stop, published before anyone asks it to.

    Held ready by supervision so that a cancellation costs no round trip
    through the conversation: whoever stops the attempt writes these bytes once
    and signals in the same breath.
    """

    data: bytes


@dataclass(frozen=True, slots=True)
class ProviderSessionEvent:
    """One step of the conversation, already in the transcript's vocabulary."""

    step: TranscriptEvent


class ProviderFilesystemEffect(StrEnum):
    """What a provider can ask to have done to one file, and nothing else."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ProviderFilesystemRequestId:
    """The identity of one file request inside one conversation.

    An ordinal the conversation counts, never an identifier a provider spelled:
    a provider that invents, repeats or omits its own id could otherwise
    address the answer meant for another request, or make two requests look
    like one -- the same reason a permission question is correlated by a minted
    id (`contracts.agent_permissions`).
    """

    call_ordinal: int

    def __post_init__(self) -> None:
        if type(self.call_ordinal) is not int or self.call_ordinal < 1:
            raise ValueError("a filesystem request ordinal counts from one")


@dataclass(frozen=True, slots=True)
class ProviderFilesystemRequest:
    """What a running provider wants done to one file, as Atelier understands it."""

    effect: ProviderFilesystemEffect
    path: Path
    request_id: ProviderFilesystemRequestId
    content: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.effect, ProviderFilesystemEffect):
            raise TypeError("a filesystem request uses the closed effect vocabulary")
        if self.content and self.effect is not ProviderFilesystemEffect.WRITE:
            raise ValueError("only a write carries content")


class ProviderFilesystemAnswer(StrEnum):
    """Whether the file this request named was reached at all."""

    ANSWERED = "answered"
    REFUSED = "refused"


class ProviderFilesystemRefusal(StrEnum):
    """Why one file request was not answered, in words a provider may be told.

    Every member names a finding about the request, never a host path: the
    provider learns that its path left the lease or named a symlink, not where
    the lease stands. `PERMISSION_REFUSED` is the one refusal that is not the
    fence's -- the path was the workspace, and the bound policy said no.
    """

    PATH_LEFT_THE_LEASE = "path-left-the-lease"
    PATH_NOT_ENCODABLE = "path-not-encodable"
    PATH_NAMED_A_SYMLINK = "path-named-a-symlink"
    PATH_CROSSED_A_MOUNT = "path-crossed-a-mount"
    FILE_NOT_FOUND = "file-not-found"
    ACCESS_DENIED = "access-denied"
    FILE_IS_HARD_LINKED = "file-is-hard-linked"
    NOT_A_REGULAR_FILE = "not-a-regular-file"
    LEASED_DIRECTORY_CHANGED = "leased-directory-changed"
    RESOLVED_FILE_CHANGED_IDENTITY = "resolved-file-changed-identity"
    FILE_EXCEEDS_THE_CEILING = "file-exceeds-the-ceiling"
    PROTECTED_PATH = "protected-path"
    PARENT_MISSING = "parent-missing"
    TARGET_IS_SYMLINK = "target-is-symlink"
    PERMISSION_REFUSED = "permission-refused"
    WORKSPACE_IO_FAILED = "workspace-io-failed"


@dataclass(frozen=True, slots=True)
class ProviderFilesystemReply:
    """What came back for exactly one file request.

    A refusal names why, so the conversation can tell the provider in its own
    error frame rather than leaving it to guess. `detail` exists for exactly
    one refusal: an unclassified I/O fault names its errno there so the
    failure stays diagnosable -- never the host path a provider must not learn.
    """

    request_id: ProviderFilesystemRequestId
    answer: ProviderFilesystemAnswer
    content: bytes = b""
    refusal: ProviderFilesystemRefusal | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        answered = self.answer is ProviderFilesystemAnswer.ANSWERED
        if answered and self.refusal is not None:
            raise ValueError("an answered file request carries no refusal")
        if not answered and self.refusal is None:
            raise ValueError("a refused file request names why")
        if self.content and not answered:
            raise ValueError("a refused file request carries no content")
        if self.detail and (
            self.refusal is not ProviderFilesystemRefusal.WORKSPACE_IO_FAILED
        ):
            raise ValueError("only a workspace I/O failure names its errno")


class ProviderFilesystemAuthority(Protocol):
    """Who authorises one file request before its effect, and keeps that answer.

    `PermissionDecider`'s sibling for files, asked from inside the fence rather
    than from the relay: only the access that resolves a path knows whether the
    path is the workspace at all. A path that is, is `decide`d -- the bound
    policy answers and the answer is kept before a byte moves. A path that is
    not, is `refuse`d -- kept as a refusal under the same revision without the
    policy being asked, because a grant recorded for a path that was never the
    workspace would be an authorisation for something the policy never named.
    """

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        """Answer exactly this question, and keep the answer before giving it."""
        ...

    def refuse(self, request: PermissionRequest) -> PermissionDecision:
        """Keep this question as refused without judging it."""
        ...


class ProviderFilesystemAccess(Protocol):
    """Who reaches a file for a running provider, and refuses what it may not.

    One seam, for the same reason `PermissionDecider` is one: the conversation
    asks and spells the answer but opens nothing, and supervision carries the
    request here and the reply back without opening anything either. A binding
    is handed the access its deployment allows this attempt; an executor that
    made its own would be granting itself the workspace.
    """

    def answer(self, request: ProviderFilesystemRequest) -> ProviderFilesystemReply:
        """Do exactly this to exactly that file, or refuse it."""
        ...


class ProviderCancellationCause(StrEnum):
    """Why an attempt is being stopped, as the side that stops it knows.

    The cause travels with the request rather than being inferred afterwards:
    a run stopped because its budget ran out and one an operator stopped by
    hand end in the same signal, and only the caller knows which happened.
    """

    OPERATOR = "operator"
    BUDGET = "budget"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class ProviderCancellationRequest:
    """This conversation asking for its own process to be stopped, and why.

    A stop the driver reaches on its own: a tool ceiling spent, a policy it
    cannot answer under. It carries the cause because the ending it will be
    told, and the outcome that ending composes, are the caller's reading of
    why -- not something a signal could say afterwards. The stop itself is the
    same one an operator asks for: the frame this conversation published is
    written once and the signal follows in the same breath.
    """

    cause: ProviderCancellationCause

    def __post_init__(self) -> None:
        if not isinstance(self.cause, ProviderCancellationCause):
            raise TypeError("a cancellation request uses the closed cause vocabulary")


@dataclass(frozen=True, slots=True)
class ProviderConversationComplete:
    """This conversation has nothing further to send, so its input may close.

    How a persistent stdio server is let go without a signal: end of file is
    what it waits for, and only the conversation knows that its last frame was
    the last one. Whatever it has already queued is written first; the child's
    standard input closes once that is drained.
    """


type ProviderConversationAction = (
    ProviderStandardInput
    | ProviderCancellationFrame
    | ProviderCancellationRequest
    | ProviderConversationComplete
    | PermissionRequest
    | ProviderFilesystemRequest
    | ProviderSessionEvent
)
"""Everything a conversation can ask of the side that owns the process."""


class ProviderConversationEnding(StrEnum):
    """How the conversation's process ended, as supervision saw it."""

    OUTPUT_ENDED = "output-ended"
    TERMINATED = "terminated"
    CANCELLED_BY_OPERATOR = "cancelled-by-operator"
    CANCELLED_FOR_BUDGET = "cancelled-for-budget"
    CANCELLED_FOR_POLICY = "cancelled-for-policy"

    @classmethod
    def of_cancellation(
        cls, cause: ProviderCancellationCause
    ) -> ProviderConversationEnding:
        """The ending a conversation is told when this cause stopped it."""

        return _ENDING_OF_CANCELLATION_CAUSE[cause]


_ENDING_OF_CANCELLATION_CAUSE = {
    ProviderCancellationCause.OPERATOR: (
        ProviderConversationEnding.CANCELLED_BY_OPERATOR
    ),
    ProviderCancellationCause.BUDGET: ProviderConversationEnding.CANCELLED_FOR_BUDGET,
    ProviderCancellationCause.POLICY: ProviderConversationEnding.CANCELLED_FOR_POLICY,
}


class ProviderTerminalReason(StrEnum):
    """Why this conversation is over, in its own reading of what happened.

    `PROTOCOL_FAULT` is the exchange having stopped being one -- framing lost,
    an answer nobody asked for, a promised message never sent. A provider that
    broke never decided to stop, so it is never `CANCELLED_BY_PROVIDER`, the
    one arm that carries a provider's own word.
    """

    ENDED = "ended"
    POLICY_REFUSED = "policy-refused"
    BUDGET_EXHAUSTED = "budget-exhausted"
    CANCELLED_BY_OPERATOR = "cancelled-by-operator"
    CANCELLED_BY_PROVIDER = "cancelled-by-provider"
    PROTOCOL_FAULT = "protocol-fault"


@dataclass(frozen=True, slots=True)
class ProviderTerminalOutcome:
    """How one conversation ended, typed, beside the bytes its process left.

    An exit code and a transcript say what a process did, never why it stopped:
    a refusal latched after a permission was denied, an exhausted budget and an
    operator's own stop all end a process the same way. The provider's own stop
    reason is kept as data on the one arm that has one, never parsed into
    meaning here.
    """

    reason: ProviderTerminalReason
    provider_stop_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ProviderTerminalReason):
            raise TypeError("a terminal outcome uses the closed reason vocabulary")
        if self.provider_stop_reason and (
            self.reason is not ProviderTerminalReason.CANCELLED_BY_PROVIDER
        ):
            raise ValueError("only a provider that stopped itself names a stop reason")
        if len(self.provider_stop_reason) > MAXIMUM_AGENT_FIELD_CHARACTERS:
            raise ValueError("a provider stop reason exceeds the agent field bound")


@dataclass(frozen=True, slots=True)
class ProviderConversationClosing:
    """What a conversation leaves behind: how it really ended, and its last steps.

    Kept apart from the actions it publishes while running, and typed so that
    an ended conversation cannot ask for anything: there is nothing left to
    write to, so whatever it says here is evidence rather than a reply.
    """

    outcome: ProviderTerminalOutcome
    steps: tuple[ProviderSessionEvent, ...] = ()


class ProviderConversation(Protocol):
    """One provider's wire format as a state machine, with no I/O of its own.

    It reads bytes and answers with actions; writing them, deciding a
    permission, reaching a file and enforcing a bound all belong to whoever
    holds the process. That separation is what keeps a provider's parser out of
    the supervision loop: nothing here can delay a cancellation, and nothing
    here can answer a permission question or open a file under an authority it
    chose itself.
    """

    @property
    def bounds(self) -> ProviderConversationBounds: ...

    @property
    def incomplete_frame_bytes(self) -> int:
        """How much of an unfinished frame this conversation is still holding.

        Supervision holds that buffer to `maximum_incomplete_frame_bytes`, and
        only the conversation knows which of the bytes it read are still an
        unfinished sentence: output it consumed and answered nothing to is not
        a frame that never ends.
        """
        ...

    def open(self) -> tuple[ProviderConversationAction, ...]:
        """Say the first thing, before this process has said anything.

        A protocol whose first frame is the caller's -- a handshake, a session
        it opens -- spells that frame here, so the lifecycle and the request
        ids it counts stay inside the conversation. A command's own standard
        input would be the other place to put it, and there the conversation
        could neither correlate the answer nor number what follows.
        """
        ...

    def receive_output(self, chunk: bytes) -> tuple[ProviderConversationAction, ...]:
        """Read exactly these output bytes and say what they ask for."""
        ...

    def input_written(
        self, written_bytes: int
    ) -> tuple[ProviderConversationAction, ...]:
        """This many of the bytes it asked for have physically reached the child.

        Counted cumulatively over everything this conversation queued, and said
        only once the bytes left the parent for the child's own pipe. A
        conversation that has to know what the provider really has -- which
        permission answer it may still take back, what its prepared stop frame
        should now say -- cannot learn that from a queue it does not own.
        """
        ...

    def answer_permission(self, decision: PermissionDecision) -> ProviderStandardInput:
        """Spell this answer in the provider's own wire format."""
        ...

    def answer_filesystem(
        self, reply: ProviderFilesystemReply
    ) -> ProviderStandardInput:
        """Spell what came back for one file request, refusal included."""
        ...

    def finish(self, ending: ProviderConversationEnding) -> ProviderConversationClosing:
        """Close this conversation, knowing how its process ended.

        A half frame is one reason this exists: what an ended process leaves
        unfinished is evidence when the output simply ran out, and something
        else again when supervision stopped it mid-sentence. The other is the
        outcome -- only the conversation knows whether the stop it was told
        about was the end of a refusal it had already latched.
        """
        ...
