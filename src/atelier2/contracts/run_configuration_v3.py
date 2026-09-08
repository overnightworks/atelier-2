from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agents import AgentBindingSetHash
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    DeterministicNodeV3,
    SubworkflowNodeV3,
    VersionedReference,
    WorkflowGraphV3,
    WorkflowNodeV3,
)

type ReferenceChain = tuple[VersionedReference, ...]


@dataclass(frozen=True, slots=True)
class ReferenceSite:
    """Where one versioned reference sits, in the words the author wrote it in.

    `field` almost always names the exact field the author wrote. One
    exception stands: when a V3 node pins two tool grants together (#1101),
    `evaluate_executability.py::resolve_document_references` renames the
    resolved site of the second, effect-shaped one from the authored `tools`
    to the synthesized `EFFECT_SHAPED_TOOL_RESOLUTION_FIELD` token, at the one
    place its capability is already read. This keeps the durable node-execution
    binding (`bind_node_execution.py`) reading resolved references as pure
    data -- it still finds at most one entry under `tools`, so the
    already-hashed one-grant case is untouched -- rather than adding a second
    schema to the frozen `run-configuration-revision/v1` preimage this type
    feeds (`_framed_reference`), which `run_fork_store.py` reconstructs field
    by field from exactly those bytes and nothing else.
    """

    field: str
    node: str | None = None
    entry: str | None = None
    chain: ReferenceChain = ()

    def __post_init__(self) -> None:
        if self.field == "":
            raise ValueError("a reference site names the field carrying the reference")

    def __str__(self) -> str:
        subject = (
            "the workflow document" if self.node is None else f"node {self.node!r}"
        )
        entry = "" if self.entry is None else f" {self.entry!r}"
        return f"{_reached_by(self.chain)}{subject} field {self.field!r}{entry}"


def _reached_by(chain: ReferenceChain) -> str:
    if not chain:
        return ""
    return " > ".join(_named(reference) for reference in chain) + " > "


def _named(reference: VersionedReference) -> str:
    return f"{reference.ref}@{reference.revision}"


@dataclass(frozen=True, slots=True)
class DeclaredReference:
    """One versioned reference a document declares, and the registry it names."""

    site: ReferenceSite
    kind: RevisionKind
    reference: VersionedReference


class ReferenceRefusalReason(StrEnum):
    """Why one versioned reference cannot bind, as a stable token."""

    MALFORMED_REVISION = "malformed_revision"
    UNPUBLISHED_REVISION = "unpublished_revision"
    REVISION_KIND_MISMATCH = "revision_kind_mismatch"
    RESOLVED_REVISION_MISMATCH = "resolved_revision_mismatch"
    UNUSABLE_SCHEMA_DOCUMENT = "unusable_schema_document"
    UNREDEEMABLE_TOOL_GRANT = "unredeemable_tool_grant"
    UNUSABLE_BUDGET_DOCUMENT = "unusable_budget_document"
    UNUSABLE_ADAPTER_OPERATION = "unusable_adapter_operation"


@dataclass(frozen=True, slots=True)
class ReferenceRefusal:
    """The named subject of one resolution refusal: node, field, and reference."""

    reason: ReferenceRefusalReason
    site: ReferenceSite
    kind: RevisionKind
    reference: VersionedReference
    detail: str

    def __str__(self) -> str:
        return (
            f"{self.site} reference {_named(self.reference)} of kind "
            f"{self.kind.value}: {self.detail} [{self.reason.value}]"
        )


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    """One declared reference, bound to the exact published revision it resolved to."""

    site: ReferenceSite
    kind: RevisionKind
    reference: VersionedReference
    revision_hash: PublishedRevisionHash


class RunConfigurationRevisionHash(Sha256Hash):
    """The immutable identity of one complete run-configuration snapshot."""


@dataclass(frozen=True)
class RunConfigurationRevision:
    """Everything a V3 run binds before it starts, as one immutable snapshot.

    The role matrix is carried by its existing `AgentBindingSetHash` rather than
    copied, so the V2 owner of role-to-configuration binding stays the only one.
    Entries are ordered by their exact framed bytes, so the same matrix reached in
    any order is the same revision.
    """

    workflow_revision_hash: WorkflowRevisionHash
    binding_set_hash: AgentBindingSetHash
    resolutions: tuple[ResolvedReference, ...] = ()
    preimage: bytes = field(init=False)
    revision_hash: RunConfigurationRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        references = tuple(sorted(self.resolutions, key=_framed_reference))
        object.__setattr__(self, "resolutions", references)
        preimage = frame(
            "run-configuration-revision/v1",
            self.workflow_revision_hash.value.encode("ascii"),
            self.binding_set_hash.value.encode("ascii"),
            frame(
                "run-configuration-references/v1",
                *(_framed_reference(entry) for entry in references),
            ),
        )
        object.__setattr__(self, "preimage", preimage)
        object.__setattr__(
            self, "revision_hash", RunConfigurationRevisionHash.of(preimage)
        )


def _framed_chain(chain: ReferenceChain) -> bytes:
    return frame(
        "reference-chain/v1",
        *(
            frame(
                "reference-chain-entry/v1",
                reference.ref.encode("utf-8"),
                reference.revision.encode("utf-8"),
            )
            for reference in chain
        ),
    )


def _framed_reference(entry: ResolvedReference) -> bytes:
    site = entry.site
    return frame(
        "run-configuration-reference/v1",
        entry.kind.value.encode("ascii"),
        site.field.encode("utf-8"),
        (site.node or "").encode("utf-8"),
        (site.entry or "").encode("utf-8"),
        _framed_chain(site.chain),
        entry.reference.ref.encode("utf-8"),
        entry.reference.revision.encode("utf-8"),
        entry.revision_hash.value.encode("ascii"),
    )


def declared_references(
    graph: WorkflowGraphV3, chain: ReferenceChain = ()
) -> tuple[DeclaredReference, ...]:
    """Every versioned reference one document declares, with the registry it names.

    Workflow references remain authored declarations; the current root run does not
    resolve them into a child configuration.
    """
    return tuple(_walk(graph, chain))


def _walk(graph: WorkflowGraphV3, chain: ReferenceChain) -> Iterator[DeclaredReference]:
    for graph_input in graph.graph_inputs:
        yield DeclaredReference(
            ReferenceSite("graph_inputs.schema", None, graph_input.name, chain),
            RevisionKind.SCHEMA,
            graph_input.schema_reference,
        )
    for node in graph.nodes:
        yield from _walk_node(node, chain)


def _declared_reference(
    node: WorkflowNodeV3,
    chain: ReferenceChain,
    field_name: str,
    kind: RevisionKind,
    reference: VersionedReference,
    entry: str | None = None,
) -> DeclaredReference:
    return DeclaredReference(
        ReferenceSite(field_name, node.id, entry, chain), kind, reference
    )


def _agent_context_references(
    node: AgentNodeV3, chain: ReferenceChain
) -> Iterator[DeclaredReference]:
    """An agent node's available-context sources and their read operations."""

    for available in node.available_context:
        yield _declared_reference(
            node,
            chain,
            "available_context.source",
            RevisionKind.CONTEXT_SOURCE,
            available.source,
            available.name,
        )
        for read_operation in available.read_operations:
            yield _declared_reference(
                node,
                chain,
                "available_context.read_operations",
                RevisionKind.READ_OPERATION,
                read_operation,
                available.name,
            )


def _agent_policy_references(
    node: AgentNodeV3, chain: ReferenceChain
) -> Iterator[DeclaredReference]:
    """An agent node's kept profile, policy, budget, and retry references."""

    for reference, field_name, kind in (
        (node.profile, "profile", RevisionKind.PROFILE),
        (node.policy, "policy", RevisionKind.POLICY),
        (node.budget, "budget", RevisionKind.BUDGET_POLICY),
        (node.retry, "retry", RevisionKind.RETRY_POLICY),
    ):
        if reference is not None:
            yield _declared_reference(node, chain, field_name, kind, reference)


def _agent_grant_references(
    node: AgentNodeV3, chain: ReferenceChain
) -> Iterator[DeclaredReference]:
    """An agent node's granted skill and tool references."""

    for references, field_name, kind in (
        (node.skills, "skills", RevisionKind.SKILL),
        (node.tools, "tools", RevisionKind.TOOL),
    ):
        for reference in references:
            yield _declared_reference(node, chain, field_name, kind, reference)


def _walk_node(
    node: WorkflowNodeV3, chain: ReferenceChain
) -> Iterator[DeclaredReference]:
    def declared(
        field_name: str,
        kind: RevisionKind,
        reference: VersionedReference,
        entry: str | None = None,
    ) -> DeclaredReference:
        return _declared_reference(node, chain, field_name, kind, reference, entry)

    if node.cancellation is not None:
        yield declared(
            "cancellation", RevisionKind.CANCELLATION_POLICY, node.cancellation
        )
    for output in node.outputs:
        yield declared(
            "outputs.schema", RevisionKind.SCHEMA, output.schema_reference, output.name
        )
    if not isinstance(node, SubworkflowNodeV3):
        for required in node.required_context:
            yield declared(
                "required_context.source",
                RevisionKind.CONTEXT_SOURCE,
                VersionedReference(
                    ref=required.source.ref, revision=required.source.revision
                ),
                required.name,
            )
    if isinstance(node, AgentNodeV3):
        yield from _agent_context_references(node, chain)
        yield from _agent_policy_references(node, chain)
        yield from _agent_grant_references(node, chain)
    if isinstance(node, DeterministicNodeV3):
        yield declared(
            "operation", RevisionKind.DETERMINISTIC_OPERATION, node.operation
        )
        if node.retry is not None:
            yield declared("retry", RevisionKind.RETRY_POLICY, node.retry)
    if isinstance(node, ActionNodeV3):
        yield declared("operation", RevisionKind.ADAPTER_OPERATION, node.operation)
    if isinstance(node, SubworkflowNodeV3):
        yield declared("workflow", RevisionKind.WORKFLOW, node.workflow)
        if node.budget is not None:
            yield declared("budget", RevisionKind.BUDGET_POLICY, node.budget)
