"""Durable V3 node records: request, context package, artifact, and receipt.

ADR 0006 owns the preimage lists and the four persisted dispositions. This module
is the typed production owner of those hashes. Nested sequences use their own
frame domain, the same encoding rule as `run-agent-bindings/v1`. Alias history,
policy activation, supersede markers, and store tables are not this owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agents import AgentExecutionCapability
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

MAXIMUM_KIND_TOKEN_CHARACTERS = 64


class DeclaredContextPackageHash(Sha256Hash):
    """The immutable identity of one `context-package-declared/v3` manifest.

    Its own type because it is its own record. ADR 0006's complete package is
    still unbuilt, and when it lands it brings `DeclaredContextPackageHash` back with its
    own frame; keeping the final name free is what stops the incomplete record
    from having quietly occupied it.
    """


class NodeExecutionRequestHash(Sha256Hash):
    """The immutable identity of one node-execution-request/v3."""


class NodeArtifactHash(Sha256Hash):
    """The immutable identity of one node-artifact/v3."""


class NodeReceiptHash(Sha256Hash):
    """The immutable identity of one node-receipt/v3."""


class NodeKindV3(StrEnum):
    """The five V3 node kinds a request may bind."""

    AGENT = "agent"
    DETERMINISTIC = "deterministic"
    WAIT = "wait"
    SUBWORKFLOW = "subworkflow"
    ACTION = "action"


class PersistedReceiptDisposition(StrEnum):
    """The four stored terminal dispositions. `stale` is never one of them."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class NodeReceiptReason(StrEnum):
    """The stable tokens a receipt's reason opens with.

    ADR 0006 gives a receipt a disposition and a reason, and a reason a reader
    can only match by prose is a reason nobody can query. The token is the part
    this product owns; what a schema owner said about one value follows it and
    belongs to that owner.
    """

    OUTPUT_ACCEPTED = "output-accepted"
    EFFECT_CONFIRMED = "effect-confirmed"
    OUTPUT_SCHEMA_REFUSED = "output-schema-refused"
    PROCESS_EXITED_UNSUCCESSFULLY = "process-exited-unsuccessfully"
    PROCESS_OUTPUT_LIMIT_EXCEEDED = "process-output-limit-exceeded"
    PROCESS_SUPERVISION_FAILED = "process-supervision-failed"
    AGENT_REFUSED = "agent-refused"
    PROJECT_VERIFICATION_FAILED = "project-verification-failed"
    CANDIDATE_CAPTURE_FAILED = "candidate-capture-failed"
    CANDIDATE_UNCHANGED = "candidate-unchanged"
    PRODUCED_VALUE_REFUSED = "produced-value-refused"
    CANCELLED_BY_OPERATOR = "cancelled-by-operator"
    """The running node's own receipt when an operator's run-cancel ends it.

    Deliberately not `run-cancelled`: ADR 0006 already gives that token to the
    `blocked` receipt a *not-yet-started* node gets when a run cancel cascades
    over the ready set (`docs/decisions/0006-node-vocabulary.md`). This token
    names the other half -- the node that was actually running -- so the two
    never collide once both have writers.
    """


def node_receipt_reason(token: NodeReceiptReason, verdict: str | None = None) -> str:
    """One receipt reason: the owned token, and the words of whoever judged.

    Composed in one place so a reader can split on the same separator every
    writer used, rather than on the habit of the writer that happened to run.
    """
    return token.value if verdict is None else f"{token.value}: {verdict}"


class ProjectedDeliveryStatus(StrEnum):
    """What an input envelope carries, including the projected `stale` status."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    STALE = "stale"


def _ascii_hash(value: Sha256Hash) -> bytes:
    return value.value.encode("ascii")


def _optional_ascii_hash(value: PublishedRevisionHash | None) -> bytes:
    return b"" if value is None else _ascii_hash(value)


def _require_node_id(node_id: str) -> None:
    if node_id == "":
        raise ValueError("a V3 node record names a nonempty node id")


@dataclass(frozen=True)
class DeclaredContextPackage:
    """Exact manifest bytes under `context-package-declared/v3`.

    The identifier is the same on the outside as on the inside on purpose: an
    outer frame carrying the final domain name around a declared body would
    narrow the record only where nobody reads it, and the hash -- the part that
    travels into a receipt -- would still claim the complete package.
    """

    manifest: bytes
    package_hash: DeclaredContextPackageHash = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "package_hash",
            DeclaredContextPackageHash.of(
                frame("context-package-declared/v3", self.manifest)
            ),
        )


@dataclass(frozen=True)
class ContextPackageMember:
    """One `required_context` entry a node declared, bound to what it resolved to.

    This is the **declared** half of ADR 0006's package member, and deliberately
    not the whole of it. The ADR's member is `(name, source revision, selector,
    content hash)`, and the content hash is the one part no owner can produce:
    nothing materializes a selector against its source. Carrying the field empty
    would mint a complete identity out of an incomplete record, so it is absent
    here and the frame says which half this is. Members keep their declared
    order, so a re-ordered or swapped member is visible in the hash.
    """

    name: str
    source_revision: PublishedRevisionHash
    selector: str

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("a context-package member names a nonempty entry")
        if self.selector == "":
            raise ValueError("a context-package member names a nonempty selector")
        if not isinstance(self.source_revision, PublishedRevisionHash):
            raise TypeError("a context-package member names a typed source revision")

    def framed(self) -> bytes:
        return frame(
            "context-package-declared-member/v3",
            self.name.encode("utf-8"),
            _ascii_hash(self.source_revision),
            self.selector.encode("utf-8"),
        )


@dataclass(frozen=True)
class RunInput:
    """One order a run was started with: a name, its schema, and exact bytes.

    This is material, not a revision. #38's first sentence is that a published
    workflow serves every order without a new revision, and that is only true
    while the order travels beside the document rather than inside it.

    It is a **separate form** from `ContextPackageMember` on purpose. That one is
    `required_context`-shaped -- a source revision and a selector into it -- and
    an order has neither: it has a schema it satisfies and bytes somebody
    supplied. Writing an order into that shape would put a source revision where
    a schema revision is, which is a lie in a preimage rather than a reuse.
    """

    name: str
    schema_revision: PublishedRevisionHash
    value: bytes
    value_hash: Sha256Hash = field(init=False)

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("a run input names a nonempty order")
        if not isinstance(self.schema_revision, PublishedRevisionHash):
            raise TypeError("a run input names a typed schema revision")
        if not isinstance(self.value, bytes):
            raise TypeError("a run input carries exact bytes")
        object.__setattr__(self, "value_hash", Sha256Hash.of(self.value))


@dataclass(frozen=True)
class MaterializedOrderMember:
    """One order the run was started with, as the package member that binds it.

    ADR 0006's member is `(name, source revision, selector, content hash)`, and
    `ContextPackageMember` above carries the declared half because no owner
    materializes a selector against its source. An order is the one member that
    *is* material: the start stored its exact bytes, so the content hash the ADR
    asks for is in hand before the package is written.

    It is a separate member form rather than a filled-in `ContextPackageMember`
    for the reason `RunInput` already gives: what pins an order is a schema
    revision, and writing that where a source revision belongs would be a lie in
    a preimage rather than a reuse.
    """

    name: str
    schema_revision: PublishedRevisionHash
    value_hash: Sha256Hash

    @classmethod
    def of(cls, order: RunInput) -> MaterializedOrderMember:
        """The member form of one stored order, hashed as the store keeps it."""
        return cls(order.name, order.schema_revision, order.value_hash)

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("an order member names a nonempty order")
        if not isinstance(self.schema_revision, PublishedRevisionHash):
            raise TypeError("an order member names a typed schema revision")
        if not isinstance(self.value_hash, Sha256Hash):
            raise TypeError("an order member names a typed content hash")

    def framed(self) -> bytes:
        return frame(
            "context-package-order-member/v3",
            self.name.encode("utf-8"),
            _ascii_hash(self.schema_revision),
            _ascii_hash(self.value_hash),
        )


@dataclass(frozen=True)
class DeliveredOutput:
    """One value a node produced, handed to the node whose author reads it.

    It is what the producing node's completion wrote -- the exact payload bytes
    of its `AGENT_COMPLETED` event -- addressed by the output name its author
    declared. The hash is taken here so the reader's job carries an identity for
    the value rather than a copy nobody can check against the event it came from.

    It is a **separate form** from `RunInput`: an order is material somebody
    supplied and the run stores, while this is work the run produced. They meet
    only in the job, where each announces itself by name.
    """

    node_id: str
    output_name: str
    value: bytes
    value_hash: Sha256Hash = field(init=False)

    def __post_init__(self) -> None:
        if self.node_id == "" or self.output_name == "":
            raise ValueError("a delivered output names its node and its output")
        if not isinstance(self.value, bytes):
            raise TypeError("a delivered output carries exact bytes")
        object.__setattr__(self, "value_hash", Sha256Hash.of(self.value))


def declared_context_package_of(
    workflow_revision_hash: WorkflowRevisionHash,
    run_id: RunId,
    node_id: str,
    members: tuple[ContextPackageMember, ...],
    orders: tuple[MaterializedOrderMember, ...] = (),
) -> DeclaredContextPackage:
    """The declared context one node was assembled with, as its own container.

    **This is not ADR 0006's complete package, and its frame says so.** The ADR
    binds the manifest to material written once before START -- the selected
    requirement, epic and story material, the decisions and contracts, the source
    and target SHAs, the prior receipts and the provenance -- and no owner
    produces any of that yet. What can be produced today is what the document
    declared and the frozen configuration resolved, so that is what this frames,
    under `context-package-declared/v3`. When the missing material and the
    selector materializer land, the complete package is authored under its own
    identity rather than by quietly widening this one.

    The container is what the hash covers, so the workflow revision and the node
    it was assembled for are part of it: the same members reached for another
    node are another package.

    Orders are the half that is material. They carry their own frame beside the
    declared members rather than inside it, because they answer a different
    question -- what somebody supplied, against what the document pinned -- and a
    reader that could not tell the two apart would read a content hash as a
    selector nobody materialized.
    """
    _require_node_id(node_id)
    return DeclaredContextPackage(
        frame(
            "context-package-declared-body/v3",
            _ascii_hash(workflow_revision_hash),
            run_id.value.encode("utf-8"),
            node_id.encode("utf-8"),
            frame(
                "context-package-declared-members/v3",
                *(member.framed() for member in members),
            ),
            frame(
                "context-package-order-members/v3",
                *(order.framed() for order in orders),
            ),
        )
    )


@dataclass(frozen=True)
class AvailableContextGrant:
    """One `available_context` grant: name, source, and allowed read operations."""

    name: str
    source_revision: PublishedRevisionHash
    read_operation_revisions: tuple[PublishedRevisionHash, ...] = ()

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("an available-context grant names a nonempty entry")
        if not isinstance(self.source_revision, PublishedRevisionHash):
            raise TypeError("an available-context grant names a typed source revision")
        if any(
            not isinstance(revision, PublishedRevisionHash)
            for revision in self.read_operation_revisions
        ):
            raise TypeError("available-context read operations must be typed revisions")

    def framed(self) -> bytes:
        return frame(
            "available-context-entry/v3",
            self.name.encode("utf-8"),
            _ascii_hash(self.source_revision),
            frame(
                "available-context-reads/v3",
                *(_ascii_hash(revision) for revision in self.read_operation_revisions),
            ),
        )


@dataclass(frozen=True)
class InputReceiptBinding:
    """The non-succeeded envelope form: upstream node, disposition, reason, hash."""

    node_id: str
    disposition: PersistedReceiptDisposition
    reason: str
    receipt_hash: NodeReceiptHash

    def __post_init__(self) -> None:
        _require_node_id(self.node_id)
        if not isinstance(self.disposition, PersistedReceiptDisposition):
            raise TypeError("an input receipt names a persisted disposition")
        if self.reason == "":
            raise ValueError("an input receipt names a nonempty reason")
        if not isinstance(self.receipt_hash, NodeReceiptHash):
            raise TypeError("an input receipt names a typed receipt hash")

    def framed(self) -> bytes:
        return frame(
            "input-envelope-receipt/v3",
            self.node_id.encode("utf-8"),
            self.disposition.value.encode("ascii"),
            self.reason.encode("utf-8"),
            _ascii_hash(self.receipt_hash),
        )


def _require_typed_envelope_header(envelope: InputEnvelope) -> None:
    """Whether the envelope's status and input name are well-formed."""

    if not isinstance(envelope.status, ProjectedDeliveryStatus):
        raise TypeError("an input envelope names its status through the contract")
    if envelope.name == "":
        raise ValueError("an input envelope names a nonempty input")


def _require_succeeded_envelope_shape(envelope: InputEnvelope) -> None:
    """Whether a succeeded envelope carries its value and no stale receipt."""

    if envelope.schema_revision is None or envelope.value_hash is None:
        raise ValueError("a succeeded input envelope carries schema and value")
    if envelope.receipt is not None:
        raise ValueError("a succeeded input envelope carries no receipt")
    if (envelope.source_event_hash is None) != (envelope.source_receipt_hash is None):
        raise ValueError(
            "a reused succeeded input names both source hashes or neither"
        )


def _require_persisted_envelope_shape(envelope: InputEnvelope) -> None:
    """Whether a non-succeeded envelope carries exactly its persisted receipt."""

    if envelope.receipt is None:
        raise ValueError("a non-succeeded input envelope names the upstream receipt")
    if envelope.schema_revision is not None or envelope.value_hash is not None:
        raise ValueError("a non-succeeded input envelope carries no schema or value")
    if (
        envelope.source_event_hash is not None
        or envelope.source_receipt_hash is not None
    ):
        raise ValueError("a non-succeeded input carries no reused success source")
    if (
        envelope.status is not ProjectedDeliveryStatus.STALE
        and envelope.status.value != envelope.receipt.disposition.value
    ):
        raise ValueError(
            "a non-stale input envelope status matches the persisted disposition"
        )


@dataclass(frozen=True)
class InputEnvelope:
    """One bound input: succeeded value, or the named non-success receipt."""

    status: ProjectedDeliveryStatus
    name: str
    schema_revision: PublishedRevisionHash | None = None
    value_hash: Sha256Hash | None = None
    receipt: InputReceiptBinding | None = None
    source_event_hash: Sha256Hash | None = None
    source_receipt_hash: NodeReceiptHash | None = None

    def __post_init__(self) -> None:
        _require_typed_envelope_header(self)
        if self.status is ProjectedDeliveryStatus.SUCCEEDED:
            _require_succeeded_envelope_shape(self)
        else:
            _require_persisted_envelope_shape(self)

    def framed(self) -> bytes:
        inherited_source = (
            ()
            if self.source_event_hash is None or self.source_receipt_hash is None
            else (
                _ascii_hash(self.source_event_hash),
                _ascii_hash(self.source_receipt_hash),
            )
        )
        return frame(
            "input-envelope/v3",
            self.status.value.encode("ascii"),
            self.name.encode("utf-8"),
            _optional_ascii_hash(self.schema_revision),
            b"" if self.value_hash is None else _ascii_hash(self.value_hash),
            b"" if self.receipt is None else self.receipt.framed(),
            *inherited_source,
        )


@dataclass(frozen=True)
class BoundNodeRevisions:
    """The eight resolved revision ids the request binds in ADR 0006 order."""

    agent_configuration: PublishedRevisionHash | None = None
    profile: PublishedRevisionHash | None = None
    skill: PublishedRevisionHash | None = None
    tool: PublishedRevisionHash | None = None
    policy: PublishedRevisionHash | None = None
    budget: PublishedRevisionHash | None = None
    retry: PublishedRevisionHash | None = None
    cancellation: PublishedRevisionHash | None = None

    def framed_fields(self) -> tuple[bytes, ...]:
        return (
            _optional_ascii_hash(self.agent_configuration),
            _optional_ascii_hash(self.profile),
            _optional_ascii_hash(self.skill),
            _optional_ascii_hash(self.tool),
            _optional_ascii_hash(self.policy),
            _optional_ascii_hash(self.budget),
            _optional_ascii_hash(self.retry),
            _optional_ascii_hash(self.cancellation),
        )


@dataclass(frozen=True)
class DeclaredOutput:
    """One declared output name and the schema revision it must satisfy."""

    name: str
    schema_revision: PublishedRevisionHash

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("a declared output names a nonempty output")
        if not isinstance(self.schema_revision, PublishedRevisionHash):
            raise TypeError("a declared output names a typed schema revision")

    def framed(self) -> bytes:
        return frame(
            "declared-output/v3",
            self.name.encode("utf-8"),
            _ascii_hash(self.schema_revision),
        )


@dataclass(frozen=True)
class NodeExecutionRequest:
    """One exact `node-execution-request/v3` preimage."""

    workflow_revision_hash: WorkflowRevisionHash
    run_configuration_revision_hash: RunConfigurationRevisionHash
    run_id: RunId
    node_id: str
    context_package_hash: DeclaredContextPackageHash
    available_context: tuple[AvailableContextGrant, ...]
    kind: NodeKindV3
    mode: AgentExecutionCapability | None
    inputs: tuple[InputEnvelope, ...]
    bound_revisions: BoundNodeRevisions
    declared_outputs: tuple[DeclaredOutput, ...]
    preimage: bytes = field(init=False)
    request_hash: NodeExecutionRequestHash = field(init=False)

    def __post_init__(self) -> None:
        _require_node_id(self.node_id)
        if not isinstance(self.workflow_revision_hash, WorkflowRevisionHash):
            raise TypeError("a node request names a typed workflow revision")
        if not isinstance(
            self.run_configuration_revision_hash, RunConfigurationRevisionHash
        ):
            raise TypeError("a node request names a typed run-configuration revision")
        if not isinstance(self.run_id, RunId):
            raise TypeError("a node request names a typed run id")
        if not isinstance(self.context_package_hash, DeclaredContextPackageHash):
            raise TypeError("a node request names a typed context-package hash")
        if not isinstance(self.kind, NodeKindV3):
            raise TypeError("a node request names its kind through the contract")
        if self.mode is not None and not isinstance(
            self.mode, AgentExecutionCapability
        ):
            raise TypeError("a node request names its mode through the contract")
        if self.kind is NodeKindV3.AGENT and self.mode is None:
            raise ValueError("an agent request binds a mode")
        if self.kind is not NodeKindV3.AGENT and self.mode is not None:
            raise ValueError("only an agent request binds a mode")
        preimage = frame(
            "node-execution-request/v3",
            _ascii_hash(self.workflow_revision_hash),
            _ascii_hash(self.run_configuration_revision_hash),
            self.run_id.value.encode("utf-8"),
            self.node_id.encode("utf-8"),
            _ascii_hash(self.context_package_hash),
            frame(
                "available-context/v3",
                *(grant.framed() for grant in self.available_context),
            ),
            self.kind.value.encode("ascii"),
            b"" if self.mode is None else self.mode.value.encode("ascii"),
            frame(
                "input-envelopes/v3",
                *(envelope.framed() for envelope in self.inputs),
            ),
            *self.bound_revisions.framed_fields(),
            frame(
                "declared-outputs/v3",
                *(output.framed() for output in self.declared_outputs),
            ),
        )
        object.__setattr__(self, "preimage", preimage)
        object.__setattr__(self, "request_hash", NodeExecutionRequestHash.of(preimage))


@dataclass(frozen=True)
class NodeArtifact:
    """One named output value under `node-artifact/v3`."""

    run_id: RunId
    node_id: str
    node_execution_id: NodeExecutionId
    output_name: str
    schema_revision: PublishedRevisionHash
    value: bytes
    value_hash: Sha256Hash = field(init=False)
    artifact_hash: NodeArtifactHash = field(init=False)

    def __post_init__(self) -> None:
        _require_node_id(self.node_id)
        if self.output_name == "":
            raise ValueError("a node artifact names a nonempty output")
        if not isinstance(self.run_id, RunId):
            raise TypeError("a node artifact names a typed run id")
        if not isinstance(self.node_execution_id, NodeExecutionId):
            raise TypeError("a node artifact names a typed node execution")
        if not isinstance(self.schema_revision, PublishedRevisionHash):
            raise TypeError("a node artifact names a typed schema revision")
        value_hash = Sha256Hash.of(self.value)
        object.__setattr__(self, "value_hash", value_hash)
        object.__setattr__(
            self,
            "artifact_hash",
            NodeArtifactHash.of(
                frame(
                    "node-artifact/v3",
                    self.run_id.value.encode("utf-8"),
                    self.node_id.encode("utf-8"),
                    _ascii_hash(self.node_execution_id),
                    self.output_name.encode("utf-8"),
                    _ascii_hash(self.schema_revision),
                    self.value,
                    _ascii_hash(value_hash),
                )
            ),
        )


@dataclass(frozen=True)
class ReceiptOutput:
    """One declared output as the receipt records it: name, schema, value hash."""

    name: str
    schema_revision: PublishedRevisionHash
    value_hash: Sha256Hash

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("a receipt output names a nonempty output")
        if not isinstance(self.schema_revision, PublishedRevisionHash):
            raise TypeError("a receipt output names a typed schema revision")
        if not isinstance(self.value_hash, Sha256Hash):
            raise TypeError("a receipt output names a typed value hash")

    def framed(self) -> bytes:
        return frame(
            "node-receipt-output/v3",
            self.name.encode("utf-8"),
            _ascii_hash(self.schema_revision),
            _ascii_hash(self.value_hash),
        )


@dataclass(frozen=True)
class NodeReceipt:
    """One terminal `node-receipt/v3`. `stale` cannot be written.

    `schema_revision` and `value_hash` are the schema identity a judgment used.
    They are absent on a receipt nobody judged and on an older row that predates
    the fields; both fields are present together, or neither is.
    """

    node_execution_id: NodeExecutionId
    disposition: PersistedReceiptDisposition
    reason: str
    request_hash: NodeExecutionRequestHash
    context_package_hash: DeclaredContextPackageHash
    outputs: tuple[ReceiptOutput, ...]
    schema_revision: PublishedRevisionHash | None = None
    value_hash: Sha256Hash | None = None
    receipt_hash: NodeReceiptHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.node_execution_id, NodeExecutionId):
            raise TypeError("a node receipt names a typed node execution")
        if not isinstance(self.disposition, PersistedReceiptDisposition):
            raise TypeError("a node receipt names a persisted disposition")
        if self.reason == "":
            raise ValueError("a node receipt names a nonempty reason")
        if not isinstance(self.request_hash, NodeExecutionRequestHash):
            raise TypeError("a node receipt names a typed request hash")
        if not isinstance(self.context_package_hash, DeclaredContextPackageHash):
            raise TypeError("a node receipt names a typed context-package hash")
        if (
            self.disposition is not PersistedReceiptDisposition.SUCCEEDED
            and self.outputs
        ):
            raise ValueError("a non-succeeded receipt carries no output values")
        if (self.schema_revision is None) != (self.value_hash is None):
            raise ValueError("schema identity is both fields or neither")
        if self.schema_revision is not None and not isinstance(
            self.schema_revision, PublishedRevisionHash
        ):
            raise TypeError("a node receipt names a typed schema revision")
        if self.value_hash is not None and not isinstance(self.value_hash, Sha256Hash):
            raise TypeError("a node receipt names a typed value hash")
        identity: tuple[bytes, ...] = ()
        if self.schema_revision is not None and self.value_hash is not None:
            identity = (
                _ascii_hash(self.schema_revision),
                _ascii_hash(self.value_hash),
            )
        object.__setattr__(
            self,
            "receipt_hash",
            NodeReceiptHash.of(
                frame(
                    "node-receipt/v3",
                    _ascii_hash(self.node_execution_id),
                    self.disposition.value.encode("ascii"),
                    self.reason.encode("utf-8"),
                    _ascii_hash(self.request_hash),
                    _ascii_hash(self.context_package_hash),
                    frame(
                        "node-receipt-outputs/v3",
                        *(output.framed() for output in self.outputs),
                    ),
                    # Published V3 receipts hash this empty subframe; removing
                    # its historical bytes would rename them.
                    frame("node-receipt-access/v3"),
                    *identity,
                )
            ),
        )
