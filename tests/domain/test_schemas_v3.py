from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

import pytest
from referencing.exceptions import Unretrievable

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    resolve_document_references,
)
from atelier2.application.resolve_references import resolve_declared_reference
from atelier2.contracts import schemas_v3
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
from atelier2.contracts.schemas_v3 import (
    MAXIMUM_SCHEMA_CONTAINER_DEPTH,
    MAXIMUM_SCHEMA_DOCUMENT_BYTES,
    MAXIMUM_SCHEMA_VALUES,
    SchemaAccepted,
    SchemaDocumentRefusal,
    SchemaRefused,
    SchemaRetrievalAttempted,
    read_schema_document,
    refuse_retrieval,
    schema_registry,
)
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolvePublishedRevisionResult,
)

A_REAL_SCHEMA = b'{"type": "object", "required": ["verdict"]}'
A_LOCAL_REFERENCE = (
    b'{"$defs": {"verdict": {"type": "string"}}, "$ref": "#/$defs/verdict"}'
)


def deeper_than_allowed() -> bytes:
    """One array nested past the container ceiling, and nothing else."""
    levels = MAXIMUM_SCHEMA_CONTAINER_DEPTH + 1
    return ("[" * levels + "]" * levels).encode("utf-8")


def wider_than_allowed() -> bytes:
    """A flat array whose entries alone exceed the value ceiling."""
    return json.dumps([0] * (MAXIMUM_SCHEMA_VALUES + 1)).encode("utf-8")


def larger_than_allowed() -> bytes:
    padding = "x" * MAXIMUM_SCHEMA_DOCUMENT_BYTES
    return json.dumps({"type": "string", "description": padding}).encode("utf-8")


@dataclass(frozen=True)
class OneRevisionRegistry:
    """A registry carrying exactly the revision under test, and nothing else."""

    revision: PublishedRevision

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        raise AssertionError("resolution never publishes")

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        if kind is self.revision.kind and revision_hash == self.revision.revision_hash:
            return PublishedRevisionFound(self.revision)
        return PublishedRevisionMissing()


def resolution_of(document: bytes, kind: RevisionKind = RevisionKind.SCHEMA):
    """What the one seam with consumers answers for these exact published bytes."""
    revision = PublishedRevision(kind, document)
    declared = DeclaredReference(
        ReferenceSite("outputs.schema", "decide", "verdict"),
        kind,
        VersionedReference(ref="verdict", revision=revision.revision_hash.value),
    )
    return resolve_declared_reference(declared, OneRevisionRegistry(revision))


THE_FIVE_THAT_BOUND_CLEANLY: tuple[tuple[str, bytes, SchemaDocumentRefusal], ...] = (
    ("prose", b"Guten Morgen", SchemaDocumentRefusal.DOCUMENT_NOT_JSON),
    (
        "a nonlocal reference",
        b'{"$ref": "https://example.com/verdict.json"}',
        SchemaDocumentRefusal.NONLOCAL_REFERENCE,
    ),
    (
        "a dynamic reference",
        b'{"$id": "urn:x", "$anchor": "a", "$dynamicRef": "#a"}',
        SchemaDocumentRefusal.FORBIDDEN_KEYWORD,
    ),
    ("bytes that are not JSON", b"\xff\xfe", SchemaDocumentRefusal.DOCUMENT_NOT_UTF8),
    (
        "a non-canonical number",
        b'{"const": NaN}',
        SchemaDocumentRefusal.NON_CANONICAL_NUMBER,
    ),
)


@pytest.mark.proves("a-schema-revision-outside-the-profile-is-refused-by-name")
@pytest.mark.parametrize(
    ("label", "document", "expected"),
    THE_FIVE_THAT_BOUND_CLEANLY,
    ids=[label for label, _, _ in THE_FIVE_THAT_BOUND_CLEANLY],
)
def test_a_schema_document_outside_the_profile_is_refused_by_name(
    label: str, document: bytes, expected: SchemaDocumentRefusal
) -> None:
    """Each of the five documents that bound cleanly before now names its own fault."""
    verdict = read_schema_document(document)

    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is expected

    refusal = resolution_of(document)

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT
    assert expected.value in str(refusal)


@pytest.mark.proves("a-schema-revision-outside-the-profile-is-refused-by-name")
@pytest.mark.parametrize(
    ("document", "expected"),
    (
        (larger_than_allowed(), SchemaDocumentRefusal.DOCUMENT_TOO_LARGE),
        (deeper_than_allowed(), SchemaDocumentRefusal.DOCUMENT_TOO_DEEP),
        (wider_than_allowed(), SchemaDocumentRefusal.TOO_MANY_VALUES),
        (
            "﻿{}".encode(),
            SchemaDocumentRefusal.DOCUMENT_CARRIES_BYTE_ORDER_MARK,
        ),
        (
            b'{"type": "object", "type": "string"}',
            SchemaDocumentRefusal.DUPLICATE_OBJECT_KEY,
        ),
        (b"{", SchemaDocumentRefusal.DOCUMENT_NOT_JSON),
        (b'{"type": 17}', SchemaDocumentRefusal.NOT_A_SCHEMA),
    ),
    ids=(
        "too large",
        "too deep",
        "too many values",
        "byte order mark",
        "duplicate object key",
        "truncated json",
        "not a draft 2020-12 schema",
    ),
)
def test_every_bound_of_the_profile_refuses_by_its_own_name(
    document: bytes, expected: SchemaDocumentRefusal
) -> None:
    verdict = read_schema_document(document)

    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is expected


def test_an_exact_number_beyond_the_profile_is_refused_by_name() -> None:
    """A valid JSON number must return a verdict, never escape the decoder."""
    literal = b"1e" + b"9" * 19

    verdict = read_schema_document(b'{"const":' + literal + b"}")

    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is SchemaDocumentRefusal.NON_CANONICAL_NUMBER
    assert verdict.subject == literal.decode()


@pytest.mark.parametrize(
    "document",
    (A_REAL_SCHEMA, A_LOCAL_REFERENCE, b"true", b'{"format": "email"}'),
    ids=("an object schema", "a local reference", "a boolean schema", "an annotation"),
)
def test_a_schema_inside_the_profile_is_accepted_and_still_binds(
    document: bytes,
) -> None:
    """The profile refuses what it names and nothing else."""
    assert isinstance(read_schema_document(document), SchemaAccepted)

    resolved = resolution_of(document)

    assert isinstance(resolved, ResolvedReference)
    assert (
        resolved.revision_hash
        == PublishedRevision(RevisionKind.SCHEMA, document).revision_hash
    )


def test_a_revision_of_another_kind_is_never_read_as_a_schema() -> None:
    """Only a `schema` reference reads its bytes; every other kind binds on identity."""
    resolved = resolution_of(b"the standing method of a builder", RevisionKind.PROFILE)

    assert isinstance(resolved, ResolvedReference)


def one_node_document_pinning(schema: PublishedRevision) -> WorkflowGraphV3:
    """The smallest document that declares one output and pins its schema."""
    document = f"""format_version: 3
name: Settle one verdict
nodes:
  - id: decide
    type: agent
    role: judge
    mode: headless
    instruction: Settle the verdict this panel was asked for.
    outputs:
      - name: verdict
        schema: {{ref: verdict_schema, revision: {schema.revision_hash.value}}}
"""
    graph = parse_workflow_document(document.encode("utf-8"))
    assert isinstance(graph, WorkflowGraphV3)
    return graph


@pytest.mark.proves("a-schema-revision-outside-the-profile-is-refused-by-name")
def test_a_document_pinning_an_unusable_schema_never_becomes_bindable() -> None:
    """The whole document is refused, not merely the one reference read alone.

    The tests above ask the resolver about a single declared reference. What a
    start asks is whether the document binds at all, and that answer must name the
    same fault -- otherwise a revision could be called bindable and then refused.
    """
    prose = PublishedRevision(RevisionKind.SCHEMA, b"the verdict a panel returns")

    refused = resolve_document_references(
        one_node_document_pinning(prose), OneRevisionRegistry(prose)
    )

    assert isinstance(refused, DocumentNotExecutable), refused
    assert refused.refusal is not None
    assert refused.refusal.reason is ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT
    assert refused.refusal.site == ReferenceSite("outputs.schema", "decide", "verdict")


THE_THREE_THAT_EVALUATION_CANNOT_SURVIVE: tuple[
    tuple[str, bytes, SchemaDocumentRefusal], ...
] = (
    (
        "an anchor no document declares",
        b'{"$ref": "#missing"}',
        SchemaDocumentRefusal.UNRESOLVABLE_REFERENCE,
    ),
    (
        "a pointer into nothing",
        b'{"$ref": "#/$defs/missing"}',
        SchemaDocumentRefusal.UNRESOLVABLE_REFERENCE,
    ),
    (
        "a cycle no instance can break",
        b'{"$ref": "#"}',
        SchemaDocumentRefusal.NON_TERMINATING_REFERENCE_CYCLE,
    ),
)


@pytest.mark.proves("a-schema-revision-outside-the-profile-is-refused-by-name")
@pytest.mark.parametrize(
    ("label", "document", "expected"),
    THE_THREE_THAT_EVALUATION_CANNOT_SURVIVE,
    ids=[label for label, _, _ in THE_THREE_THAT_EVALUATION_CANNOT_SURVIVE],
)
def test_a_local_reference_that_leads_nowhere_is_refused_by_name(
    label: str, document: bytes, expected: SchemaDocumentRefusal
) -> None:
    """Locality was never enough: a ref reaching nothing is unusable, by its own name."""
    verdict = read_schema_document(document)

    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is expected

    refusal = resolution_of(document)

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT


@pytest.mark.parametrize(
    ("label", "document", "expected"),
    THE_THREE_THAT_EVALUATION_CANNOT_SURVIVE,
    ids=[label for label, _, _ in THE_THREE_THAT_EVALUATION_CANNOT_SURVIVE],
)
def test_no_accepted_schema_can_break_the_evaluator(
    label: str, document: bytes, expected: SchemaDocumentRefusal
) -> None:
    """The liveness half: nothing this profile accepts may blow the stack.

    A published revision that passes the profile and then raises RecursionError
    is not a refusal, it is an outage, so the accepted set is proven safe to
    build a validator over rather than merely well-formed.
    """
    verdict = read_schema_document(document)

    assert isinstance(verdict, SchemaRefused)


A_RECURSIVE_TREE = b'{"type": "object", "properties": {"child": {"$ref": "#"}}}'


@pytest.mark.parametrize(
    ("label", "document"),
    (
        ("a self-referencing tree", A_RECURSIVE_TREE),
        (
            "a recursion through items",
            b'{"type": "array", "items": {"$ref": "#"}}',
        ),
        (
            "a resolvable pointer into $defs",
            b'{"$defs": {"leaf": {"type": "string"}}, "$ref": "#/$defs/leaf"}',
        ),
        (
            "a pointer through an escaped key",
            b'{"$defs": {"a/b": {"type": "string"}}, "$ref": "#/$defs/a~1b"}',
        ),
    ),
    ids=("tree", "items", "defs pointer", "escaped key"),
)
def test_a_recursion_an_instance_can_break_stays_legal(
    label: str, document: bytes
) -> None:
    """The ruling's positive half, and the reason it is not simply `refuse $ref: "#"`.

    A tree is the shape this workshop's own outputs most want to declare, and it
    terminates because every application of the reference descends into a
    strictly smaller instance. Refusing it would buy the liveness fix by making
    recursion inexpressible, which is a narrower profile than the defect needs.
    """
    assert isinstance(read_schema_document(document), SchemaAccepted)


@pytest.mark.parametrize(
    ("label", "document", "expected"),
    (
        (
            "a cycle through an in-place applicator",
            b'{"allOf": [{"$ref": "#"}]}',
            SchemaDocumentRefusal.NON_TERMINATING_REFERENCE_CYCLE,
        ),
        (
            "two definitions referring to each other",
            (
                b'{"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},'
                b' "type": "object"}'
            ),
            SchemaDocumentRefusal.NON_TERMINATING_REFERENCE_CYCLE,
        ),
        (
            "an unresolvable reference below a property",
            b'{"properties": {"child": {"$ref": "#/$defs/absent"}}}',
            SchemaDocumentRefusal.UNRESOLVABLE_REFERENCE,
        ),
    ),
    ids=("in-place cycle", "mutual cycle", "unresolvable below a property"),
)
def test_the_reference_graph_is_judged_over_the_whole_document(
    label: str, document: bytes, expected: SchemaDocumentRefusal
) -> None:
    """Resolvability holds everywhere; termination holds wherever evaluation may land."""
    verdict = read_schema_document(document)

    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is expected


@pytest.mark.parametrize(
    ("label", "document", "accepted"),
    (
        ("no dialect declared", b'{"type": "string"}', True),
        (
            "exactly 2020-12",
            (
                b'{"$schema": "https://json-schema.org/draft/2020-12/schema",'
                b' "type": "string"}'
            ),
            True,
        ),
        (
            "draft-07",
            b'{"$schema": "http://json-schema.org/draft-07/schema#", "type": "string"}',
            False,
        ),
        ("nonsense", b'{"$schema": "not a dialect", "type": "string"}', False),
        ("not even a string", b'{"$schema": 7, "type": "string"}', False),
    ),
    ids=("absent", "2020-12", "draft-07", "nonsense", "not a string"),
)
def test_the_dialect_is_pinned_rather_than_reinterpreted(
    label: str, document: bytes, accepted: bool
) -> None:
    """A document announcing another dialect is refused, never read as 2020-12."""
    verdict = read_schema_document(document)

    if accepted:
        assert isinstance(verdict, SchemaAccepted)
        return
    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is SchemaDocumentRefusal.UNSUPPORTED_DIALECT


@pytest.fixture
def counted_retrievals(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every retrieval the production path attempts, in order."""
    attempted: list[str] = []
    original: Callable[[str], NoReturn] = schemas_v3.refuse_retrieval

    def counting(uri: str) -> NoReturn:
        attempted.append(uri)
        original(uri)

    monkeypatch.setattr(schemas_v3, "refuse_retrieval", counting)
    return attempted


@pytest.mark.proves("no-schema-reference-ever-leaves-the-document")
def test_no_schema_document_ever_reaches_the_retrieval_seam(
    counted_retrievals: list[str],
) -> None:
    """Reading every document in this suite attempts exactly zero retrievals."""
    documents = [document for _, document, _ in THE_FIVE_THAT_BOUND_CLEANLY]
    documents += [A_REAL_SCHEMA, A_LOCAL_REFERENCE, larger_than_allowed()]

    for document in documents:
        read_schema_document(document)

    assert counted_retrievals == []


@pytest.mark.proves("no-schema-reference-ever-leaves-the-document")
def test_the_only_retrieval_path_raises_instead_of_fetching() -> None:
    """The seam is armed, not absent: any URI at all is a loud failure."""
    with pytest.raises(SchemaRetrievalAttempted):
        refuse_retrieval("https://example.com/verdict.json")

    with pytest.raises(Unretrievable) as refused:
        schema_registry().get_or_retrieve("https://example.com/verdict.json")

    assert isinstance(refused.value.__cause__, SchemaRetrievalAttempted)
