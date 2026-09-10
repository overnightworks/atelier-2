"""Where one declared reference of a V3 document lands, or why it lands nowhere.

The verdict is per reference and carries no policy: what to do with one that lands
nowhere is the asker's. `evaluate_executability` asks it for every reference a
document pins and refuses the whole snapshot at the first that does not resolve,
which is the shape a run configuration freezes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import assert_never

from atelier2.application.bounded_process_cache import BoundedProcessCache
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.adapter_operations_v3 import (
    AdapterOperationRefused,
    read_adapter_operation_document,
)
from atelier2.contracts.budgets_v3 import (
    BudgetRevisionRefused,
    read_budget_revision_document,
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
    ResolvedReference,
    declared_references,
)
from atelier2.contracts.schemas_v3 import (
    SchemaAccepted,
    SchemaDocumentVerdict,
    SchemaRefused,
    read_schema_document,
)
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantRefused,
    read_tool_grant_document,
)
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.published_revisions import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionResolver,
    PublishedRevisionsUnavailable,
)

type SettledResolution = ResolvedReference | ReferenceRefusal
"""What a registry that answered says about one reference."""
type ReferenceResolution = SettledResolution | ReadUnavailable | DurableStateCorrupt
"""That, or the registry could not answer -- which is about the store, not the reference."""


_SCHEMA_VERDICT_CACHE_CAPACITY = 4_096
"""Comfortably above the tens of schema revisions one project holds today.

The cap bounds memory for a pathological project; it never bounds
correctness. A published schema revision is content-addressed and cannot
change once published, so an accepted verdict stays correct for the
process's whole lifetime -- there is no staleness to invalidate.
"""

# #937 round 4: profiling a described page after round 3 gave the workflow
# parse a process-lifetime cache found the dominant remaining cost was
# validating the same immutable schema document against the Draft 2020-12
# meta-schema on every request -- every node's own declared output schema,
# not only a wait node's, reaches `read_schema_document` through this
# module's `_unreadable_document`. This module-level cache
# (`BoundedProcessCache`, the same owner round 3's parsed workflow cache
# uses) is shared by every caller of `cached_schema_document`, so a schema
# pinned by many revisions, across many pages and many requests -- the
# described listing, the single-revision read and the start alike -- is
# validated once for the process rather than once per page (round 2) or once
# per request (before round 2).
#
# A refused verdict is deliberately never cached: it proved no schema value,
# and remembering it would turn one document's refusal into process-lifetime
# durable-state corruption instead of letting the next read re-examine it --
# the same reasoning round 3 already applies to a failed workflow parse.
# Refused schema documents are also rare, so the repeat cost stays small.
_SCHEMA_VERDICTS: BoundedProcessCache[PublishedRevisionHash, SchemaAccepted] = (
    BoundedProcessCache(capacity=_SCHEMA_VERDICT_CACHE_CAPACITY)
)


def cached_schema_document(
    revision_hash: PublishedRevisionHash, document: bytes
) -> SchemaDocumentVerdict:
    """`read_schema_document`, replayed from the process cache on a repeat.

    The one owner of whether a schema document is accepted or refused stays
    `read_schema_document`; this wrapper is the only place that remembers its
    answer, so every caller -- inside this module and in
    `read_workflow_revisions` -- shares one verdict per hash rather than
    deriving it a second way.

    Two callers racing on the same still-uncached hash can both miss, both
    validate and both `remember`: `BoundedProcessCache` holds no lock across
    that validation. That is fine here because `document` is the immutable
    content `revision_hash` names, so both validations read the same bytes
    and produce the same verdict -- the race can cost one duplicate
    validation, never a wrong cached answer.
    """
    accepted = _SCHEMA_VERDICTS.found(revision_hash)
    if accepted is not None:
        return accepted
    verdict = read_schema_document(document)
    if isinstance(verdict, SchemaAccepted):
        _SCHEMA_VERDICTS.remember(revision_hash, verdict)
    return verdict


def declared_through(document: WorkflowGraphV3) -> Iterator[DeclaredReference]:
    for declared in declared_references(document):
        if declared.kind is not RevisionKind.WORKFLOW:
            yield declared


def resolve_declared_reference(
    declared: DeclaredReference,
    resolver: PublishedRevisionResolver,
) -> ReferenceResolution:
    """The published revision one declared reference binds, or the named refusal."""
    resolution, _revision = resolve_declared_reference_with_revision(declared, resolver)
    return resolution


def resolve_declared_reference_with_revision(
    declared: DeclaredReference,
    resolver: PublishedRevisionResolver,
) -> tuple[ReferenceResolution, PublishedRevision | None]:
    """Resolve once, retaining exact bytes for a caller following a typed pin."""
    try:
        revision_hash = PublishedRevisionHash(declared.reference.revision)
    except ValueError:
        return (
            _refusal(
                ReferenceRefusalReason.MALFORMED_REVISION,
                declared,
                "a pinned revision must be 64 lowercase hexadecimal characters",
            ),
            None,
        )
    resolved = resolver.resolve(declared.kind, revision_hash)
    match resolved:
        case PublishedRevisionFound(revision):
            if revision.kind is not declared.kind:
                return (
                    _refusal(
                        ReferenceRefusalReason.REVISION_KIND_MISMATCH,
                        declared,
                        f"the registry answered with a {revision.kind.value} revision",
                    ),
                    revision,
                )
            if revision.revision_hash != revision_hash:
                return (
                    _refusal(
                        ReferenceRefusalReason.RESOLVED_REVISION_MISMATCH,
                        declared,
                        f"the registry answered with revision {revision.revision_hash.value}",
                    ),
                    revision,
                )
            unusable = _unreadable_document(declared, revision)
            if unusable is not None:
                return unusable, revision
            return (
                ResolvedReference(
                    declared.site, declared.kind, declared.reference, revision_hash
                ),
                revision,
            )
        case PublishedRevisionMissing():
            return (
                _refusal(
                    ReferenceRefusalReason.UNPUBLISHED_REVISION,
                    declared,
                    f"no published {declared.kind.value} revision carries this hash",
                ),
                None,
            )
        case PublishedRevisionsUnavailable(detail):
            return ReadUnavailable(detail), None
        case PortDurableStateCorrupt():
            return DurableStateCorrupt(), None
        case _ as unreachable:
            assert_never(unreachable)


def _unreadable_document(
    declared: DeclaredReference, revision: PublishedRevision
) -> ReferenceRefusal | None:
    """Whether a revision resolved to bytes that are not the thing it was read as.

    Most kinds resolve on identity alone, because nothing here knows what their
    bytes must say. Four kinds this product must read to do its job at all are
    the exception, and the reference that pins them is where the reading belongs:
    a `schema`, which a value is read against, a `tool` grant, whose capability
    decides what an attempt is going to redeem, a `budget_policy`, whose bounds
    decide how far an attempt may run, and an `adapter_operation`, whose name
    decides which effect an Action performs. Each is refused here rather than at
    the attempt, because a run that started under it has already told its author
    it would be honoured -- and for a budget that promise is the whole point: an
    unreadable one would leave an attempt unbounded while its author reads a
    bound.
    """
    if declared.kind is RevisionKind.SCHEMA:
        verdict = cached_schema_document(revision.revision_hash, revision.document)
        if isinstance(verdict, SchemaRefused):
            return _refusal(
                ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT,
                declared,
                "the published revision is not a schema this product enforces "
                f"({verdict})",
            )
        return None
    if declared.kind is RevisionKind.TOOL:
        grant = read_tool_grant_document(revision.document)
        if isinstance(grant, ToolGrantRefused):
            return _refusal(
                ReferenceRefusalReason.UNREDEEMABLE_TOOL_GRANT,
                declared,
                "the published revision is not a tool grant this runtime "
                f"redeems ({grant})",
            )
        return None
    if declared.kind is RevisionKind.BUDGET_POLICY:
        budget = read_budget_revision_document(revision.document)
        if isinstance(budget, BudgetRevisionRefused):
            return _refusal(
                ReferenceRefusalReason.UNUSABLE_BUDGET_DOCUMENT,
                declared,
                f"the published revision bounds no attempt this runtime runs ({budget})",
            )
        return None
    if declared.kind is RevisionKind.ADAPTER_OPERATION:
        operation = read_adapter_operation_document(revision.document)
        if isinstance(operation, AdapterOperationRefused):
            return _refusal(
                ReferenceRefusalReason.UNUSABLE_ADAPTER_OPERATION,
                declared,
                "the published revision is not an adapter operation this "
                f"runtime performs ({operation})",
            )
        return None
    return None


def _refusal(
    reason: ReferenceRefusalReason, declared: DeclaredReference, detail: str
) -> ReferenceRefusal:
    return ReferenceRefusal(
        reason, declared.site, declared.kind, declared.reference, detail
    )
