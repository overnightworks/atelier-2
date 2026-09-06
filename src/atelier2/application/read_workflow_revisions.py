"""Reading published workflow revisions, as decisions rather than port answers.

Both reads are one port call and one translation. What they add over calling the
port is that their result is this layer's vocabulary: a caller matches
`WorkflowRevisionNotFound` without importing the store's word for it, and a new
outcome is a new member here rather than a new type leaking into every route.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, assert_never, cast

from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    ExecutableDocument,
    ReferenceSettlementCache,
    evaluate_executability,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.application.resolve_references import cached_schema_document
from atelier2.contracts.definition_sources import RevisionProvenance
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import SchemaRefused
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from atelier2.contracts.workflows_v3 import (
    AnyWorkflowDocument,
    VersionedReference,
    WaitNodeV3,
    WorkflowGraphV3,
)
from atelier2.ports.published_revisions import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionResolver,
    PublishedRevisionResolverWithSession,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge as PortProjectionTooLarge,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionQueries,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)


@dataclass(frozen=True)
class WaitAnswerClassification:
    """One waiting node's answer schema, classified as far as a real read may.

    This is the application layer's own answer, never the API's: the API/wire
    projection may hold a port but may not match the record kind a port
    answers with (`api-port-record-problems`, `scripts/check_architecture.py`)
    -- reading a published schema's bytes to say `boolean` or `enum` is
    exactly that kind of match, so it happens here, beside the other reader of
    references this layer already owns (`resolve_declared_reference`), and
    the API receives only this plain, portless verdict.

    `string_typed` is required, never inferred from `kind` downstream: it is
    the one fact a composer needs to send an `enum` answer's `values` back
    verbatim rather than JSON-encoded (#1091 PR #1108 finding 1) -- true for
    every `string` classification and for an `enum` whose schema also names
    `type: string`, false for `boolean`, `free`, and every other `enum`.
    """

    node_id: str
    kind: Literal["boolean", "enum", "string", "free"]
    string_typed: bool
    values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WorkflowRevisionRead:
    """One published revision, with what this build says about running it.

    `not_executable_reason` is None exactly when the start path would admit
    the document as published; otherwise it carries that path's own words.
    `provenance` is where the bytes first entered the catalog, and is None for
    a revision no definition source delivered.
    """

    projection: WorkflowRevisionProjection
    not_executable_reason: str | None
    wait_answer_classifications: tuple[WaitAnswerClassification, ...] = ()
    provenance: RevisionProvenance | None = None


@dataclass(frozen=True)
class DescribedWorkflowRevision:
    """One listed revision, judged by the same rule the detail read applies."""

    projection: WorkflowRevisionProjection
    not_executable_reason: str | None
    provenance: RevisionProvenance | None = None


@dataclass(frozen=True)
class WorkflowRevisionNotFound:
    pass


@dataclass(frozen=True)
class WorkflowRevisionsDescribed:
    """One page of revisions, each with the document it was published as."""

    items: tuple[DescribedWorkflowRevision, ...]
    next_after: WorkflowRevisionHash | None


@dataclass(frozen=True)
class WorkflowRevisionsListed:
    revision_hashes: tuple[WorkflowRevisionHash, ...]
    next_after: WorkflowRevisionHash | None


type GetWorkflowRevisionResult = (
    WorkflowRevisionRead
    | WorkflowRevisionNotFound
    | ReadUnavailable
    | ProjectionTooLarge
    | DurableStateCorrupt
)
type ListWorkflowRevisionsResult = (
    WorkflowRevisionsListed | ReadUnavailable | DurableStateCorrupt | ProjectionTooLarge
)
type ListDescribedWorkflowRevisionsResult = (
    WorkflowRevisionsDescribed
    | ReadUnavailable
    | DurableStateCorrupt
    | ProjectionTooLarge
)


def get_workflow_revision(
    revision_hash: WorkflowRevisionHash,
    queries: WorkflowRevisionQueries,
    resolver: PublishedRevisionResolver,
) -> GetWorkflowRevisionResult:
    """Read one published revision, saying whether this build runs it as published.

    The resolver is required because executability is not a property of the
    bytes alone: a document pins references, and whether each one resolves is
    half of the verdict the start path gives. Classifying every wait node's
    answer schema reads through the same resolver.
    """
    match queries.get_workflow_revision(revision_hash):
        case WorkflowRevisionFound(projection, provenance):
            return describe_workflow_revision(
                projection, resolver, provenance=provenance
            )
        case WorkflowRevisionMissing():
            return WorkflowRevisionNotFound()
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def describe_workflow_revision(
    projection: WorkflowRevisionProjection,
    resolver: PublishedRevisionResolver,
    settlements: ReferenceSettlementCache | None = None,
    provenance: RevisionProvenance | None = None,
) -> WorkflowRevisionRead | ReadUnavailable | DurableStateCorrupt:
    """What this build says about one stored revision: the read's and the publication's answer alike.

    `settlements` lets a page composing many revisions
    (`list_described_workflow_revisions`) share what it already resolved about
    a reference across every revision on it; a single-revision caller leaves
    it out and gets a private cache that dies with this call. A schema
    document's own verdict needs no such page-local cache: `resolve_references`'s
    `cached_schema_document` already remembers it for the process, shared
    with the executability check just above (#937 round 4).
    """
    match evaluate_executability(projection.graph, resolver, settlements):
        case ExecutableDocument():
            not_executable_reason = None
        case DocumentNotExecutable(reason):
            not_executable_reason = reason
        case ReadUnavailable() | DurableStateCorrupt() as failed:
            return failed
        case _ as unreachable:
            assert_never(unreachable)
    classifications = _wait_answer_classifications(projection.graph, resolver)
    if isinstance(classifications, (ReadUnavailable, DurableStateCorrupt)):
        return classifications
    return WorkflowRevisionRead(
        projection, not_executable_reason, classifications, provenance
    )


def _wait_answer_classifications(
    graph: AnyWorkflowDocument,
    resolver: PublishedRevisionResolver,
) -> tuple[WaitAnswerClassification, ...] | ReadUnavailable | DurableStateCorrupt:
    if not isinstance(graph, WorkflowGraphV3):
        return ()
    classifications: list[WaitAnswerClassification] = []
    for node in graph.nodes:
        if not isinstance(node, WaitNodeV3):
            continue
        classified = _classify_wait_answer(node, resolver)
        if isinstance(classified, (ReadUnavailable, DurableStateCorrupt)):
            return classified
        classifications.append(classified)
    return tuple(classifications)


def _classify_wait_answer(
    node: WaitNodeV3,
    resolver: PublishedRevisionResolver,
) -> WaitAnswerClassification | ReadUnavailable | DurableStateCorrupt:
    """One waiting node's answer schema, classified as far as a real read may.

    The failure modes are named here rather than left to fall through to
    `free` by accident: a malformed pinned hash, a revision this build cannot
    find, a revision published under a different kind, and a revision whose
    bytes are not a schema this product enforces are the same four reasons
    `resolve_declared_reference` already refuses to *bind* a run to a
    reference, and none of them is durable corruption -- a document may name a
    schema published after itself, exactly as `orders` already echoes its own
    hull unresolved on the wire. Each of those four reasons classifies `free`,
    the honest little this reader can say, rather than refuse the whole read
    over a reference nothing has bound yet.

    `string` names the one other shape a composer must read differently:
    where the schema's own top level is exactly `type: string` and it names
    no `enum` (an `enum` still classifies `enum`, the more specific answer),
    the value a person types *is* the answer with no JSON-quoting layer
    around it (`schemas_v3.instance_for_schema` is the one door that reads it
    that way).

    `string_typed` carries that same fact onto an `enum` classification too:
    the door decides raw-versus-JSON-encoded by the schema's top-level
    `type` alone, never by whether it also names `enum` (#1091 PR #1108
    finding 1 -- `workflows/schemas/refine_ruling.json`,
    `{"type": "string", "enum": [...]}`, is exactly this shape on the live
    `refine` workflow). So a `type: string` schema's `enum` members carry
    their own raw text in `values`, not the JSON-encoded text every other
    `enum` still carries -- a wire consumer that did not know this would keep
    handing the door a JSON-quoted `"ja"` the door refuses.
    """
    schema = _resolved_schema_document(node.outputs[0].schema_reference, resolver)
    if isinstance(schema, (ReadUnavailable, DurableStateCorrupt)):
        return schema
    if not isinstance(schema, dict):
        return WaitAnswerClassification(node.id, "free", string_typed=False)
    if schema.get("type") == "boolean":
        return WaitAnswerClassification(node.id, "boolean", string_typed=False)
    string_typed = schema.get("type") == "string"
    enum = schema.get("enum")
    if isinstance(enum, list):
        return _enum_classification(node.id, enum, string_typed)
    if string_typed:
        return WaitAnswerClassification(node.id, "string", string_typed=True)
    return WaitAnswerClassification(node.id, "free", string_typed=False)


def _enum_classification(
    node_id: str, enum: list[object], string_typed: bool
) -> WaitAnswerClassification:
    """The members a composer may offer, or `free` where one has no wire spelling."""
    if string_typed:
        if all(isinstance(member, str) for member in enum):
            return WaitAnswerClassification(
                node_id,
                "enum",
                string_typed=True,
                values=cast(tuple[str, ...], tuple(enum)),
            )
        return WaitAnswerClassification(node_id, "free", string_typed=False)
    encoded = [_json_wire_scalar(member) for member in enum]
    if all(member is not None for member in encoded):
        return WaitAnswerClassification(
            node_id,
            "enum",
            string_typed=False,
            values=cast(tuple[str, ...], tuple(encoded)),
        )
    return WaitAnswerClassification(node_id, "free", string_typed=False)


def _resolved_schema_document(
    reference: VersionedReference,
    resolver: PublishedRevisionResolver,
) -> object | None | ReadUnavailable | DurableStateCorrupt:
    """This reference's own accepted schema, or None where nothing here can read one.

    A `bool` top-level schema (`true` or `false`) is a legal Draft 2020-12
    document and is returned as-is; only a `dict` carries `type` or `enum`, so
    the caller's own `isinstance(schema, dict)` is what actually distinguishes
    a classifiable schema from one this reader still declines to guess at.

    `cached_schema_document` (`resolve_references`) remembers a schema this
    process already read and validated by its revision hash: a published
    schema's bytes never change, so any wait node anywhere -- on this
    revision, a later one, a different page, or a different request -- pinning
    the same hash gets that verdict without a second parse and validation,
    the same verdict `evaluate_executability` already settled just above for
    this same document (#937 round 4). The reference still resolves through
    the caller's own session every time: what this cache saves is the Draft
    2020-12 meta-schema validation, the cost profiling found dominant, never
    the one session's own lookup. Caching is keyed by the *requested* hash,
    so the resolver's own answer is checked against it before anything is
    remembered: a resolver that ever answered a different revision than the
    one asked for would otherwise poison this hash's process-lifetime entry
    with a stranger's schema.
    """
    try:
        revision_hash = PublishedRevisionHash(reference.revision)
    except ValueError:
        return None  # not a well-formed pinned hash at all
    match resolver.resolve(RevisionKind.SCHEMA, revision_hash):
        case PublishedRevisionFound() as resolved:
            pass
        case PublishedRevisionMissing():
            return None  # no published schema revision carries this hash yet
        case PublishedRevisionsUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if resolved.revision.kind is not RevisionKind.SCHEMA:
        return None  # this hash names a revision of a different published kind
    if resolved.revision.revision_hash != revision_hash:
        return None  # the registry answered a different revision than the one pinned
    verdict = cached_schema_document(revision_hash, resolved.revision.document)
    if isinstance(verdict, SchemaRefused):
        return None  # published bytes are not a schema this product enforces
    return verdict.schema


def _json_wire_scalar(value: object) -> str | None:
    """The exact JSON text of one schema-authored scalar, or None where it is not one.

    An `enum` member that is itself a list or object is not something a
    decision button can hold, so it does not classify here -- the caller falls
    back to `free` for the whole schema rather than a button that cannot carry
    what it names.
    """
    if value is None or isinstance(value, bool | str | int):
        return json.dumps(value)
    if isinstance(value, Decimal):
        return str(value)
    return None


def list_workflow_revisions(
    after: WorkflowRevisionHash | None,
    limit: int,
    queries: WorkflowRevisionQueries,
) -> ListWorkflowRevisionsResult:
    match queries.list_workflow_revisions(after, limit):
        case WorkflowRevisionPage(revision_hashes, next_after):
            return WorkflowRevisionsListed(revision_hashes, next_after)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def list_described_workflow_revisions(
    after: WorkflowRevisionHash | None,
    limit: int,
    budget: EnrichedPageBudget,
    queries: WorkflowRevisionQueries,
    resolver: PublishedRevisionResolverWithSession,
) -> ListDescribedWorkflowRevisionsResult:
    """One page of revisions that carries what each document says about itself.

    The budget is the composition's decision rather than the caller's, so no
    route can widen what one page is allowed to read from the store. Each item
    is judged executable by the same rule the detail read and the start apply,
    so a listing never promises a start the service then refuses. `resolver`
    is required to open a session because this read composes many lookups
    into one page: an adapter that cannot open one is a wiring defect this
    signature refuses to hide behind a silent one-lookup-at-a-time fallback
    (#937) -- every reference the page resolves runs through the one session
    opened for the whole page, never a fresh lookup per reference. The same
    reasoning applies one level up, in the settlement cache every item on the
    page shares: a tool grant, budget policy or adapter operation many of the
    page's revisions pin identically is read and resolved once for the whole
    page rather than once per revision that pins it (#937 round 2). A
    schema's own verdict needs no page-local cache of its own:
    `resolve_references.cached_schema_document` already remembers it for the
    whole process, across every page and every request (#937 round 4).
    """

    match queries.list_described_workflow_revisions(after, limit, budget):
        case DescribedWorkflowRevisionPage(items, next_after):
            settlements: ReferenceSettlementCache = {}
            with resolver.resolver_session() as page_resolver:
                described: list[DescribedWorkflowRevision] = []
                for listed in items:
                    read = describe_workflow_revision(
                        listed.projection, page_resolver, settlements
                    )
                    if isinstance(read, (ReadUnavailable, DurableStateCorrupt)):
                        return read
                    described.append(
                        DescribedWorkflowRevision(
                            listed.projection,
                            read.not_executable_reason,
                            listed.provenance,
                        )
                    )
                return WorkflowRevisionsDescribed(tuple(described), next_after)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
