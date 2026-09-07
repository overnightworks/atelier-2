"""What a document published as a `tool` revision grants, and what redeeming it leaves.

ADR 0006 makes a node's `tools` entry a versioned reference into the `tool`
registry, so a capability is never a mark an author writes: it is a published
revision the document pins by hash, exactly like the schema of an output. Bytes
published under the name `tool` are a grant only because someone called them one,
so this module is the reading that turns them into one -- a pure function over
bytes, asked once by the reference resolution that binds a run.

The vocabulary is closed. A grant naming anything outside it is refused where it
was declared rather than resolved and then silently unredeemed, because a run
started under a capability nothing performs would tell its author that the
atelier did what the document asked.

Three capabilities live here under two redemption shapes.
`RUN_PROJECT_VERIFICATION` is synchronous and exec-shaped -- a command, an exit
code, a hash of what it said -- redeemed inside the attempt's own lease.
`OPEN_PR` and `PUSH_ATELIER_COMMIT` are external effects, redeemed through the
same durable, retryable `EffectAdapter` an Action node drives and answered with
an `EffectReceipt` rather than an exit code. `redeems_as_platform_effect` is the
one owner of that split, so the two runtimes never disagree about which shape a
capability is, and the exec-shaped redemption receipt below is kept only for the
exec-shaped capability.

The redemption receipt is the other half: what the attempt that redeemed the
grant actually ran, how it ended, and what it said. It is a record of its own
rather than a field of the agent receipt, because the two answer different
questions -- the agent receipt says what the provider produced, and an exit code
sitting beside those bytes would read as the provider's.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self, assert_never

from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import VersionedReference

MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES = 4_096
"""How large a grant document may be. It names one capability; nothing needs more."""

MAXIMUM_VERIFICATION_COMMAND_BYTES = 4_096
"""How long the exact argv of one verification may be, as its durable record holds it."""

MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES = 2_048
"""How much of pytest's own summary line one failed verification's receipt keeps.

Mirrors `MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES` (#1137): a receipt is a
sentence an operator reads at a glance, not a log, and a project's own test
runner is free to compose a summary of any length even though pytest's own is
short in practice.
"""


class ToolGrantCapability(StrEnum):
    """The closed set of capabilities a published tool grant may name.

    A capability enters here together with the runtime that performs it, so the
    set and the performance never disagree. `redeems_as_platform_effect` names
    which of the two redemption shapes owns each member, because an exec-shaped
    verification and an external effect cannot honestly share one receipt.
    """

    RUN_PROJECT_VERIFICATION = "run-project-verification"
    OPEN_PR = "open-pr"
    PUSH_ATELIER_COMMIT = "push-atelier-commit"


def redeems_as_platform_effect(capability: ToolGrantCapability) -> bool:
    """Whether this capability is redeemed as an external platform effect.

    An effect-shaped capability is redeemed through the durable `EffectAdapter`
    an Action node already drives, after the attempt succeeds, and answers with
    an `EffectReceipt`; an exec-shaped one runs inside the attempt's lease and
    answers with an exit code. The binding carries only the exec-shaped grant
    -- an effect-shaped grant needs no `project_source` and is read straight
    from the immutable graph where its effect is prepared -- so this predicate
    is the one place that decides which redemption a capability takes.
    """
    match capability:
        case ToolGrantCapability.OPEN_PR | ToolGrantCapability.PUSH_ATELIER_COMMIT:
            return True
        case ToolGrantCapability.RUN_PROJECT_VERIFICATION:
            return False
    assert_never(capability)


class ToolGrantCapabilityNotRedeemed(RuntimeError):
    """A pinned grant names a capability no redeemer in this runtime performs.

    `read_tool_grant_document` already refuses any capability outside this
    module's closed vocabulary before a run ever binds one, so a legitimately
    constructed `DeclaredToolGrant` names a member every reader agrees on. This
    exception is the same refusal at the other boundary the invariant names:
    the exec-shaped redeemer asks, once more, which redeemer this exact
    capability reaches, so a capability routed to it that it does not perform --
    the effect-shaped `OPEN_PR` reaching the verification redeemer, or a later
    capability with no redeemer at all -- is refused by name here rather than
    performed as though it had asked for whichever redeemer that runtime already
    had.
    """

    def __init__(self, capability: ToolGrantCapability) -> None:
        super().__init__(f"no redeemer in this runtime performs {capability.value!r}")
        self.capability = capability


class ToolGrantRefusal(StrEnum):
    """Why published bytes are not a tool grant, as a stable token."""

    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_NOT_UTF8 = "document_not_utf8"
    NOT_A_GRANT_OBJECT = "not_a_grant_object"
    MISSING_CAPABILITY = "missing_capability"
    UNKNOWN_CAPABILITY = "unknown_capability"
    UNKNOWN_FIELD = "unknown_field"


@dataclass(frozen=True, slots=True)
class ToolGrantAccepted:
    """These bytes grant exactly this capability."""

    capability: ToolGrantCapability
    operation: VersionedReference | None = None


@dataclass(frozen=True, slots=True)
class ToolGrantRefused:
    """These bytes are not a grant this runtime redeems, and why."""

    reason: ToolGrantRefusal
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.reason.value}{suffix}"


type ToolGrantVerdict = ToolGrantAccepted | ToolGrantRefused

_CAPABILITY_FIELD = "capability"
_OPERATION_FIELD = "operation"


def read_tool_grant_document(document: bytes) -> ToolGrantVerdict:
    """Whether these exact published bytes grant a capability this runtime redeems."""
    decoded = _decoded_grant_object(document)
    if isinstance(decoded, ToolGrantRefused):
        return decoded
    named = decoded.get(_CAPABILITY_FIELD)
    if not isinstance(named, str):
        return ToolGrantRefused(
            ToolGrantRefusal.MISSING_CAPABILITY,
            f"a tool grant names the capability it grants under {_CAPABILITY_FIELD}",
        )
    try:
        capability = ToolGrantCapability(named)
    except ValueError:
        return ToolGrantRefused(
            ToolGrantRefusal.UNKNOWN_CAPABILITY,
            f"no runtime here redeems {named!r}",
        )
    expected = (
        frozenset((_CAPABILITY_FIELD, _OPERATION_FIELD))
        if capability is ToolGrantCapability.PUSH_ATELIER_COMMIT
        else frozenset((_CAPABILITY_FIELD,))
    )
    unknown = sorted(set(decoded) - expected)
    if unknown:
        if _OPERATION_FIELD in unknown:
            return ToolGrantRefused(
                ToolGrantRefusal.NOT_A_GRANT_OBJECT,
                "only a push grant pins an adapter operation",
            )
        return ToolGrantRefused(
            ToolGrantRefusal.UNKNOWN_FIELD,
            f"{capability.value} does not declare {', '.join(unknown)}",
        )
    if set(decoded) != expected:
        return ToolGrantRefused(
            ToolGrantRefusal.NOT_A_GRANT_OBJECT,
            "push-atelier-commit pins one adapter operation",
        )
    if capability is not ToolGrantCapability.PUSH_ATELIER_COMMIT:
        return ToolGrantAccepted(capability)
    return _accepted_with_pinned_operation(capability, decoded[_OPERATION_FIELD])


def _decoded_grant_object(document: bytes) -> dict[str, object] | ToolGrantRefused:
    if len(document) > MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES:
        return ToolGrantRefused(
            ToolGrantRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return ToolGrantRefused(ToolGrantRefusal.DOCUMENT_NOT_UTF8, broken.reason)
    try:
        decoded = json.loads(text)
    except ValueError as broken:
        return ToolGrantRefused(ToolGrantRefusal.NOT_A_GRANT_OBJECT, str(broken))
    if not isinstance(decoded, dict):
        return ToolGrantRefused(
            ToolGrantRefusal.NOT_A_GRANT_OBJECT,
            f"a tool grant is an object, not {type(decoded).__name__}",
        )
    return decoded


def _accepted_with_pinned_operation(
    capability: ToolGrantCapability, operation: object
) -> ToolGrantVerdict:
    """A push grant names the one adapter operation it pins, by ref and exact revision."""
    if not isinstance(operation, dict) or set(operation) != {"ref", "revision"}:
        return ToolGrantRefused(
            ToolGrantRefusal.NOT_A_GRANT_OBJECT,
            "a pinned adapter operation carries ref and revision",
        )
    ref = operation["ref"]
    revision = operation["revision"]
    try:
        if not isinstance(ref, str) or not ref or not isinstance(revision, str):
            raise ValueError
        PublishedRevisionHash(revision)
    except (TypeError, ValueError):
        return ToolGrantRefused(
            ToolGrantRefusal.NOT_A_GRANT_OBJECT,
            "a pinned adapter operation names a nonempty ref and exact revision hash",
        )
    return ToolGrantAccepted(capability, VersionedReference(ref=ref, revision=revision))


@dataclass(frozen=True, slots=True)
class DeclaredToolGrant:
    """The published grant one node declared, as the run froze it."""

    revision_hash: PublishedRevisionHash
    capability: ToolGrantCapability
    operation: VersionedReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision_hash, PublishedRevisionHash):
            raise TypeError("a declared tool grant names its published revision")
        if not isinstance(self.capability, ToolGrantCapability):
            raise TypeError("a declared tool grant uses the closed vocabulary")
        push = self.capability is ToolGrantCapability.PUSH_ATELIER_COMMIT
        if push != (self.operation is not None):
            raise ValueError("only a push grant pins one adapter operation")


class ToolRedemptionReceiptHash(Sha256Hash):
    """The immutable identity of one `tool-redemption-receipt/v1`."""


@dataclass(frozen=True)
class ToolRedemptionReceipt:
    """What redeeming one tool grant ran, how it ended, and what it said.

    The output is kept as a hash rather than as bytes: what this record proves is
    that exactly this command produced exactly this answer, and the answer of a
    project verification is a build log whose size no author declared.
    """

    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    attempt_id: AgentAttemptId
    tool_revision_hash: PublishedRevisionHash
    capability: ToolGrantCapability
    command: tuple[str, ...]
    exit_code: int
    standard_output_hash: Sha256Hash
    receipt_hash: ToolRedemptionReceiptHash = field(init=False)

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("a tool redemption receipt names the node it belongs to")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        ):
            raise ValueError(
                "tool redemption execution identity differs from its node binding"
            )
        if not isinstance(self.capability, ToolGrantCapability):
            raise TypeError("a tool redemption receipt uses the closed vocabulary")
        if not self.command or any(not argument for argument in self.command):
            raise ValueError("a redeemed verification command is nonempty throughout")
        if type(self.exit_code) is not int:
            raise TypeError("a redeemed verification exit code must be an integer")
        if not -MAXIMUM_SIGNED_INT64 - 1 <= self.exit_code <= MAXIMUM_SIGNED_INT64:
            raise ValueError("a redeemed verification exit code must fit signed int64")
        object.__setattr__(self, "receipt_hash", self._hash())

    @property
    def satisfied_the_project(self) -> bool:
        """Whether the project's own command was satisfied by what it saw.

        The one owner of that reading. A nonzero exit is not weaker proof of the
        same thing -- it is the opposite fact, and it ends the attempt under its
        own code. Every writer that keeps a redemption and every caller that
        decides what to do after one asks here, so no two of them can disagree
        about what "the check passed" means.
        """

        return self.exit_code == 0

    def _hash(self) -> ToolRedemptionReceiptHash:
        return ToolRedemptionReceiptHash.of(
            frame(
                "tool-redemption-receipt/v1",
                self.node_execution_id.value.encode("ascii"),
                self.run_id.value.encode("utf-8"),
                self.workflow_revision_hash.value.encode("ascii"),
                self.node_id.encode("utf-8"),
                self.attempt_id.value.encode("ascii"),
                self.tool_revision_hash.value.encode("ascii"),
                self.capability.value.encode("ascii"),
                frame(
                    "tool-redemption-command/v1",
                    *(argument.encode("utf-8") for argument in self.command),
                ),
                struct.pack(">q", self.exit_code),
                self.standard_output_hash.value.encode("ascii"),
            )
        )

    @classmethod
    def of(
        cls,
        node_execution_id: NodeExecutionId,
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        node_id: str,
        attempt_id: AgentAttemptId,
        grant: DeclaredToolGrant,
        command: tuple[str, ...],
        exit_code: int,
        standard_output_hash: Sha256Hash,
    ) -> Self:
        return cls(
            node_execution_id,
            run_id,
            workflow_revision_hash,
            node_id,
            attempt_id,
            grant.revision_hash,
            grant.capability,
            command,
            exit_code,
            standard_output_hash,
        )
