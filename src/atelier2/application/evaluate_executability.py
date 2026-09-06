"""Whether this build runs a published document as it stands -- one answer for every asker.

The start, the detail read, the described listing and the publication answer
all ask it. Deciding it in more than one place let a listing call a revision
executable that the start then refused under the same name (#701): the read
side judged the authored form alone, the start also resolved every pinned
reference. Now both rules are applied here, in this order, and nowhere else.
The resolutions an executable document collects are exactly the references a
run configuration freezes, so the start consumes this answer rather than
resolving a second time.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.application.resolve_references import (
    ReferenceResolution,
    SettledResolution,
    declared_through,
    resolve_declared_reference_with_revision,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceSite,
    ResolvedReference,
)
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantAccepted,
    ToolGrantCapability,
    read_tool_grant_document,
    redeems_as_platform_effect,
)
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    VersionedReference,
    WorkflowGraphV3,
    what_a_v3_document_still_waits_for,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionResolver,
)


@dataclass(frozen=True)
class ExecutableDocument:
    """This build runs the document; these are the references it resolved on the way."""

    resolutions: tuple[ResolvedReference, ...] = ()


@dataclass(frozen=True)
class DocumentNotExecutable:
    """What keeps this build from starting the document, in words fit for its author.

    `refusal` is the reference that did not resolve, when that is the reason;
    a form nothing binds carries none.
    """

    reason: str
    refusal: ReferenceRefusal | None = None


type Executability = (
    ExecutableDocument | DocumentNotExecutable | ReadUnavailable | DurableStateCorrupt
)

type ReferenceSettlementCache = dict[
    tuple[RevisionKind, PublishedRevisionHash],
    tuple[SettledResolution, PublishedRevision | None],
]
"""What this build already decided about one resolved reference's content.

Keyed by `(kind, revision_hash)` rather than by the declaring site, because a
published revision is content-addressed and immutable: the same hash always
names the same bytes, so whether it resolves -- and, for a schema, tool
grant, budget policy or adapter operation, whether its bytes are usable --
never depends on which node or document pinned it (#937 round 2: profiling a
described page found this exact read and validation as the dominant cost,
run once per reference with nothing shared across the many revisions one
page lists). A caller composing many documents into one page passes the same
cache to every one of them so a schema several revisions share is read and
validated once; a caller judging a single document leaves it out and gets a
private cache that dies with the call.

An unpublished-revision refusal is never stored here (`_cacheable`): that
hash may still be published moments later, and a cached miss would then keep
answering stale.
"""


def evaluate_executability(
    graph: AnyWorkflowDocument,
    resolver: PublishedRevisionResolver,
    settlements: ReferenceSettlementCache | None = None,
) -> Executability:
    """The two rules, in order: every authored form is bound, every reference resolves.

    A V1 or V2 document is executed whole and pins nothing, so it is executable
    without a lookup. A V3 document is refused at the first thing that stands in
    the way, because the author fixes one thing at a time and the start can
    admit nothing before all of them are fixed. `settlements` lets a caller
    that judges many documents in one composed read share what each already
    settled about a reference's content across all of them; a caller with
    none of its own gets a cache scoped to this one call.
    """
    if not isinstance(graph, WorkflowGraphV3):
        return ExecutableDocument()
    waiting = what_a_v3_document_still_waits_for(graph)
    if waiting is not None:
        return DocumentNotExecutable(waiting)
    cache: ReferenceSettlementCache = {} if settlements is None else settlements
    resolved = resolve_document_references(graph, resolver, cache)
    if not isinstance(resolved, ExecutableDocument):
        return resolved
    refusal = _looped_platform_effect_grant_refusal(graph, resolver)
    return resolved if refusal is None else DocumentNotExecutable(refusal)


def _looped_platform_effect_grant_refusal(
    graph: WorkflowGraphV3, resolver: PublishedRevisionResolver
) -> str | None:
    """Name a loop member whose grant needs a round-aware external marker.

    The generic effect key already carries the round, but GitHub's durable
    idempotency marker is the canonical request hash. Repeated identical output
    would therefore read back the prior round's pull request as this round's
    receipt. The start refuses before it writes a run or an external effect.
    """
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        loop = graph.loop_of(node.id)
        if loop is None:
            continue
        if _pins_a_platform_effect_grant(node, resolver):
            return (
                f"node {node.id!r} is a member of loop {loop.id!r} and pins "
                "an effect grant; this runtime has no round-aware external "
                "marker contract"
            )
    return None


def _pins_a_platform_effect_grant(
    node: AgentNodeV3, resolver: PublishedRevisionResolver
) -> bool:
    for reference in node.tools:
        try:
            revision_hash = PublishedRevisionHash(reference.revision)
        except ValueError:
            continue
        resolved = resolver.resolve(RevisionKind.TOOL, revision_hash)
        if not isinstance(resolved, PublishedRevisionFound):
            continue
        grant = read_tool_grant_document(resolved.revision.document)
        if isinstance(grant, ToolGrantAccepted) and redeems_as_platform_effect(
            grant.capability
        ):
            return True
    return False


EFFECT_SHAPED_TOOL_RESOLUTION_FIELD = "tools:effect-shaped"
"""The site field a *second* `tools` resolution carries when it is effect-shaped.

A node's `tools` are declared under the one authored field `tools`
(`run_configuration_v3.py`), and up to one of each shape resolves from there.
`bind_node_execution.py`'s durable request still binds exactly one `tools` id
per node (#1101 plan review: "no schema hop") -- the exec-shaped grant, read
inside the attempt's own lease. Where a node pins only its historical one
grant, effect-shaped or not, that grant keeps the authored field name exactly
as it always did. Only where a node pins two -- the new shape this record
adds -- is the effect-shaped one's *resolved* site renamed to this token, at
the one place its capability is already read, so `bind_node_execution.py`
keeps reading resolved references as pure data and still finds at most one
under the authored field name. The effect-shaped grant stays frozen in
`RunConfigurationRevision.resolutions` under this name for the run's own
record, and is read back directly from the immutable workflow revision when
its effect is prepared (`agent_effect_grants.py`).
"""


def resolve_document_references(
    graph: WorkflowGraphV3,
    resolver: PublishedRevisionResolver,
    settlements: ReferenceSettlementCache | None = None,
) -> Executability:
    """The second rule alone: every reference the document pins resolves, or the first that does not.

    Callable on its own because a document's references are bound whether or not
    every form in it is one this runtime executes yet -- the run-configuration
    snapshot is the same snapshot for both -- while the evaluation above only
    reaches here for a form it does execute. `settlements` is `evaluate_executability`'s
    own parameter, threaded through unchanged.
    """
    cache: ReferenceSettlementCache = {} if settlements is None else settlements
    resolutions: list[ResolvedReference] = []
    redeemed_grant_shapes: set[tuple[str, bool]] = set()
    for declared in declared_through(graph, SubworkflowBinding()):
        resolution, revision = _settled_resolution(declared, resolver, cache)
        match resolution:
            case ResolvedReference():
                pass
            case ReferenceRefusal():
                return DocumentNotExecutable(public_reason(resolution), resolution)
            case ReadUnavailable() | DurableStateCorrupt():
                return resolution
            case _ as unreachable:
                assert_never(unreachable)
        if revision is None or declared.kind is not RevisionKind.TOOL:
            resolutions.append(resolution)
            continue
        pinned = _pinned_tool_grant_resolutions(
            graph,
            declared,
            resolution,
            revision,
            resolver,
            cache,
            redeemed_grant_shapes,
        )
        if not isinstance(pinned, tuple):
            return pinned
        resolutions.extend(pinned)
    return ExecutableDocument(tuple(resolutions))


def _pinned_tool_grant_resolutions(
    graph: WorkflowGraphV3,
    declared: DeclaredReference,
    resolution: ResolvedReference,
    revision: PublishedRevision,
    resolver: PublishedRevisionResolver,
    cache: ReferenceSettlementCache,
    redeemed_grant_shapes: set[tuple[str, bool]],
) -> (
    tuple[ResolvedReference, ...]
    | DocumentNotExecutable
    | ReadUnavailable
    | DurableStateCorrupt
):
    """The tool reference itself and, for a push grant, the adapter operation it pins."""
    grant = read_tool_grant_document(revision.document)
    if not isinstance(grant, ToolGrantAccepted):
        return (resolution,)
    conflict = _second_grant_of_one_shape(
        redeemed_grant_shapes, declared.site.node, grant.capability
    )
    if conflict is not None:
        return DocumentNotExecutable(conflict)
    resolution = _sited_by_grant_shape(graph, declared, resolution, grant.capability)
    if grant.operation is None:
        return (resolution,)
    operation = _pinned_operation_resolution(declared, grant.operation, resolver, cache)
    if isinstance(operation, ResolvedReference):
        return (resolution, operation)
    return operation


def _sited_by_grant_shape(
    graph: WorkflowGraphV3,
    declared: DeclaredReference,
    resolution: ResolvedReference,
    capability: ToolGrantCapability,
) -> ResolvedReference:
    """An effect-shaped grant beside an exec-shaped one resolves under its own site.

    The durable node-execution binding binds one `tools` id per node, so where
    a node pins two grants the effect-shaped one is renamed here, at the one
    place its capability is read; `EFFECT_SHAPED_TOOL_RESOLUTION_FIELD` says
    why. A lone effect-shaped grant keeps the authored field name.
    """
    pinning_node = (
        None if declared.site.node is None else graph.node(declared.site.node)
    )
    if not (
        redeems_as_platform_effect(capability)
        and isinstance(pinning_node, AgentNodeV3)
        and len(pinning_node.tools) > 1
    ):
        return resolution
    return dataclasses.replace(
        resolution,
        site=dataclasses.replace(
            resolution.site, field=EFFECT_SHAPED_TOOL_RESOLUTION_FIELD
        ),
    )


def _pinned_operation_resolution(
    declared: DeclaredReference,
    operation: VersionedReference,
    resolver: PublishedRevisionResolver,
    cache: ReferenceSettlementCache,
) -> ResolvedReference | DocumentNotExecutable | ReadUnavailable | DurableStateCorrupt:
    transitive = DeclaredReference(
        ReferenceSite(
            "operation",
            declared.site.node,
            chain=(*declared.site.chain, declared.reference),
        ),
        RevisionKind.ADAPTER_OPERATION,
        operation,
    )
    nested, _operation_revision = _settled_resolution(transitive, resolver, cache)
    match nested:
        case ResolvedReference():
            return nested
        case ReferenceRefusal():
            return DocumentNotExecutable(public_reason(nested), nested)
        case ReadUnavailable() | DurableStateCorrupt():
            return nested
        case _ as unreachable:
            assert_never(unreachable)


def _second_grant_of_one_shape(
    redeemed_grant_shapes: set[tuple[str, bool]],
    node: str | None,
    capability: ToolGrantCapability,
) -> str | None:
    """Name the conflict when a node's grant repeats a shape it already resolved.

    A node may pin at most one exec-shaped and at most one effect-shaped grant;
    which shape a pin is is only known once its published bytes are read, so
    this is the start-level twin of the binding-time invariant
    `agent_effect_grants.py` enforces again once a run redeems the grant. `node`
    is always set for a tool reference -- only a document-level reference (no
    node) reaches here with `None`, and no document-level reference is a tool.
    """
    if node is None:
        return None
    platform_effect = redeems_as_platform_effect(capability)
    key = (node, platform_effect)
    if key in redeemed_grant_shapes:
        shape = "effect-shaped" if platform_effect else "exec-shaped"
        return f"node {node!r} pins more than one {shape} grant"
    redeemed_grant_shapes.add(key)
    return None


def _settled_resolution(
    declared: DeclaredReference,
    resolver: PublishedRevisionResolver,
    cache: ReferenceSettlementCache,
) -> tuple[ReferenceResolution, PublishedRevision | None]:
    """`resolve_declared_reference_with_revision`, replayed from `cache` on a repeat.

    The one decision this build makes about a reference's content is
    `resolve_references.py`'s own job, made exactly once here by calling it.
    That decision does not change for a repeat of the same
    `(kind, revision_hash)`: a found revision's bytes never change, and every
    refusal this caches is a fact about those bytes. Only `site` and
    `reference` are stamped fresh from `declared`, because the same content
    can be pinned from a different node or field each time.
    """
    try:
        revision_hash = PublishedRevisionHash(declared.reference.revision)
    except ValueError:
        return resolve_declared_reference_with_revision(declared, resolver)
    key = (declared.kind, revision_hash)
    settled = cache.get(key)
    if settled is not None:
        outcome, revision = settled
        replayed = dataclasses.replace(
            outcome, site=declared.site, reference=declared.reference
        )
        return replayed, revision
    resolution, revision = resolve_declared_reference_with_revision(declared, resolver)
    if isinstance(resolution, ResolvedReference | ReferenceRefusal) and _cacheable(
        resolution
    ):
        cache[key] = (resolution, revision)
    return resolution, revision


def _cacheable(settled: SettledResolution) -> bool:
    """Whether this settled resolution names immutable content rather than a registry answer that can still change.

    An unpublished-revision refusal names a hash the registry has not seen
    yet -- that hash may be published moments later, so caching it would let
    a later resolve keep replaying a stale miss. Every other settled outcome
    is decided by bytes a publish never rewrites: a resolved reference, or a
    refusal about the content the registry already returned.
    """
    return not (
        isinstance(settled, ReferenceRefusal)
        and settled.reason is ReferenceRefusalReason.UNPUBLISHED_REVISION
    )


def public_reason(refusal: ReferenceRefusal) -> str:
    """The refusal as the API may say it: the authored site and reference, a stable token.

    The refusal's own `detail` is for logs and tests -- it can quote a parser,
    which is not a sentence an author should have to read on a listing.
    """
    site = refusal.site
    reference = refusal.reference
    return (
        f"{site} pins {refusal.kind.value} reference "
        f"{reference.ref}@{reference.revision}: "
        f"{_WORDING[refusal.reason]} [{refusal.reason.value}]"
    )


_WORDING: dict[ReferenceRefusalReason, str] = {
    ReferenceRefusalReason.MALFORMED_REVISION: (
        "the pinned revision is not a 64-character lowercase hexadecimal hash"
    ),
    ReferenceRefusalReason.UNPUBLISHED_REVISION: (
        "no published revision of this kind carries this hash"
    ),
    ReferenceRefusalReason.REVISION_KIND_MISMATCH: (
        "this hash names a revision published under another kind"
    ),
    ReferenceRefusalReason.RESOLVED_REVISION_MISMATCH: (
        "the registry answered with a revision of another hash"
    ),
    ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT: (
        "the published revision is not a schema this product enforces"
    ),
    ReferenceRefusalReason.UNREDEEMABLE_TOOL_GRANT: (
        "the published revision is not a tool grant this runtime redeems"
    ),
    ReferenceRefusalReason.UNUSABLE_BUDGET_DOCUMENT: (
        "the published revision bounds no attempt this runtime runs"
    ),
    ReferenceRefusalReason.UNUSABLE_ADAPTER_OPERATION: (
        "the published revision is not an adapter operation this runtime performs"
    ),
}
