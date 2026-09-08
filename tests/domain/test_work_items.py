"""One tracker item as it stood at one read: its bytes, and what they identify."""

from __future__ import annotations

import json

import pytest

from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
    WorkItemOrderDocument,
    WorkItemScope,
    WorkItemScopeMalformed,
    read_work_item_order_document,
    work_item_order_document,
)

_ITEM = TrackerItemReference("gh:712")
_OBSERVED_AT = RecordedAt("2026-08-26T09:15:00Z")


def revision(
    body: bytes = b"work item body",
    change_marker: str = 'W/"5f2a"',
) -> ObservedWorkItemRevision:
    return ObservedWorkItemRevision(
        _ITEM,
        WorkItemKind.ISSUE,
        body,
        WorkItemChangeMarker(change_marker),
        _OBSERVED_AT,
    )


def test_the_digest_is_the_plain_sha256_a_reader_recomputes_from_the_bytes() -> None:
    """`printf 'work item body' | sha256sum` answers exactly this digest.

    The point of ADR 0010 §5's rule is that a human, or a second tool, can
    re-derive the identity from the object alone -- so the digest is the
    unframed SHA-256 over the served bytes, never an Atelier-only preimage.
    """

    assert revision().digest.value == (
        "8f17afb3a2b7301dc70f9b25e738e09243c53a957e8a02e96a81d97589bd049f"
    )


def test_appending_a_newline_to_the_body_is_a_different_revision() -> None:
    assert revision(b"work item body\n").digest != revision(b"work item body").digest


def test_the_same_bytes_read_twice_carry_the_same_digest() -> None:
    assert revision(change_marker='W/"5f2a"').digest == (
        revision(change_marker='W/"b118"').digest
    )


def test_a_body_that_is_not_the_served_bytes_is_refused() -> None:
    with pytest.raises(TypeError):
        ObservedWorkItemRevision(
            _ITEM,
            WorkItemKind.ISSUE,
            "text, not the served bytes",  # type: ignore[arg-type]
            WorkItemChangeMarker('W/"5f2a"'),
            _OBSERVED_AT,
        )


@pytest.mark.parametrize("value", ["", "x" * 1_025])
def test_a_change_marker_outside_its_bound_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        WorkItemChangeMarker(value)


def test_body_bytes_that_are_not_utf8_are_not_a_revision() -> None:
    """A revision is read back as text; bytes that are not text are not one."""

    with pytest.raises(ValueError):
        revision(b"\xff\xfe not utf-8")


def test_the_order_document_carries_the_read_a_run_has_to_reproduce() -> None:
    document = json.loads(work_item_order_document(revision(b"the item said this")))

    assert document == {
        "body": "the item said this",
        "change_marker": 'W/"5f2a"',
        "digest": revision(b"the item said this").digest.value,
        "kind": "issue",
        "observed_at": _OBSERVED_AT.value,
        "reference": _ITEM.value,
        "scope": [],
    }


def test_the_same_read_always_writes_the_same_order_bytes() -> None:
    """The value hash a run stores must not depend on dictionary order."""

    first = revision()
    second = ObservedWorkItemRevision(
        observed_at=first.observed_at,
        change_marker=first.change_marker,
        body=first.body,
        kind=first.kind,
        item=first.item,
    )
    assert work_item_order_document(first) == work_item_order_document(second)


@pytest.mark.parametrize(
    "body",
    [b"", b"plain", b"Umlaut, CRLF\r\nund kein Schluss"],
    ids=["empty", "plain", "non-ascii-with-carriage-return"],
)
def test_the_house_schema_admits_the_order_document_it_describes(body: bytes) -> None:
    """The workflow pins this schema, so the start would refuse a value it rejects."""

    schema = read_schema_document(WORK_ITEM_ORDER_SCHEMA_DOCUMENT)
    assert isinstance(schema, SchemaAccepted), schema

    verdict = read_instance_document(work_item_order_document(revision(body)), schema)

    assert isinstance(verdict, InstanceAccepted), verdict


def written(**fields: object) -> bytes:
    """The document a read writes, with exactly the named fields bent."""

    written_document = json.loads(work_item_order_document(revision()))
    return json.dumps({**written_document, **fields}).encode("utf-8")


def test_a_written_order_reads_back_as_the_document_it_was_written_from() -> None:
    """A retry compares the item, so reading one back has to be exact."""

    document = read_work_item_order_document(work_item_order_document(revision()))

    assert document == WorkItemOrderDocument(
        body="work item body",
        change_marker=WorkItemChangeMarker('W/"5f2a"'),
        digest=revision().digest,
        kind=WorkItemKind.ISSUE,
        observed_at=_OBSERVED_AT,
        reference=_ITEM,
        scope=WorkItemScope(()),
    )


def test_the_body_s_scope_round_trips_through_the_order_document() -> None:
    body = b"## Bereich\nsrc/atelier2/contracts/work_items.py\nworkflows/\n"

    document = read_work_item_order_document(work_item_order_document(revision(body)))

    assert document is not None
    assert document.scope == WorkItemScope(
        ("src/atelier2/contracts/work_items.py", "workflows")
    )


def test_a_scope_list_names_exactly_those_paths() -> None:
    body = (
        b"## Bereich\n"
        b"src/atelier2/contracts/work_items.py\n"
        b"src/atelier2/application/advance_queue.py\n"
    )
    assert WorkItemScope.from_body(body).paths == (
        "src/atelier2/application/advance_queue.py",
        "src/atelier2/contracts/work_items.py",
    )


def test_a_path_mentioned_in_prose_is_not_claimed() -> None:
    body = (
        "## Dateien\nTests nur unter `tests/domain` — nicht `tests/adapters`.\n"
    ).encode()
    assert WorkItemScope.from_body(body).paths == ()


@pytest.mark.parametrize(
    ("body", "paths"),
    [
        (b"## Bereich\na/file.py\n", ("a/file.py",)),
        (b"## Bereich\na/dir\na/dir/\n", ("a/dir",)),
        (b"## Bereich\na/dir//\n", ("a/dir",)),
        (b"## Bereich\nb/one\na/two\n", ("a/two", "b/one")),
        (b"## Bereich\na/one\na/one\n", ("a/one",)),
        (b"## Bereich\n", ()),
        (b"no scope section at all", ()),
        (b"## Bereich\na/one\n## Nachbarn\nb/two\n", ("a/one",)),
        (b"## Dateien\n`ignored/path.py`.\n## Bereich\na/one\n", ("a/one",)),
        (b"## Bereich\n.github/workflows/ci.yml\n", (".github/workflows/ci.yml",)),
        (
            b"## Bereich\na/one\n```\nnot/a/path\n```\n",
            ("a/one",),
        ),
        (
            b"```\n## Bereich\nnot/a/path\n```\n",
            (),
        ),
        (
            b"## Bereich\na/one\n~~~\nnot/a/path\n~~~\n",
            ("a/one",),
        ),
    ],
    ids=[
        "one-file",
        "directory-with-and-without-trailing-slash",
        "directory-with-multiple-trailing-slashes",
        "unsorted-becomes-sorted",
        "duplicate-collapses",
        "section-without-a-path",
        "no-scope-section",
        "stops-at-the-next-heading",
        "dateien-prose-is-not-read",
        "hidden-directory",
        "stops-at-a-code-fence",
        "heading-inside-a-fence-is-not-a-section",
        "stops-at-a-tilde-fence",
    ],
)
def test_the_scope_section_grammar_reads_exactly_its_path_lines(
    body: bytes, paths: tuple[str, ...]
) -> None:
    assert WorkItemScope.from_body(body).paths == paths


@pytest.mark.parametrize(
    "token",
    [
        "../etc/passwd",
        "a/../b",
        "with space",
        "*.py",
        "/absolute",
        "//",
        "`src/foo.py`",
        "#todo",
        "-src/foo.py",
        "a.py,b.py",
        "src/foo.py.",
    ],
    ids=[
        "leading-traversal",
        "embedded-traversal",
        "whitespace",
        "glob",
        "absolute",
        "only-separators",
        "backticks",
        "leading-hash",
        "list-marker",
        "comma-list",
        "trailing-period",
    ],
)
def test_a_scope_list_line_that_is_not_a_relative_path_is_a_named_error(
    token: str,
) -> None:
    with pytest.raises(WorkItemScopeMalformed) as error:
        WorkItemScope.from_body(f"## Bereich\n{token}\n".encode())

    assert error.value.token == token


def test_two_reads_of_one_item_read_back_as_the_same_item() -> None:
    edited = read_work_item_order_document(
        work_item_order_document(revision(b"edited since"))
    )

    assert edited is not None
    assert edited.reference == _ITEM


@pytest.mark.parametrize(
    "document",
    [
        b"not json",
        b"[]",
        b'{"body": "no other field"}',
        b"\xff",
        written(digest="0" * 64),
        written(digest="not a digest"),
        written(kind="merge_request"),
        written(observed_at="yesterday"),
        written(observed_at="2026-02-31T09:15:00Z"),
        written(reference="g" * 1_025),
        written(reference=""),
        written(change_marker=""),
        written(extra="field"),
        written(scope="src/atelier2/contracts/work_items.py"),
        written(scope=[1]),
        written(scope=["b", "a"]),
        written(scope=["a", "a"]),
    ],
    ids=[
        "not-json",
        "not-an-object",
        "incomplete",
        "not-utf8",
        "digest-that-is-not-the-body's",
        "digest-that-is-not-a-digest",
        "a-kind-no-tracker-answers",
        "an-instant-that-is-not-one",
        "an-instant-no-calendar-has",
        "a-reference-longer-than-one-can-be",
        "an-empty-reference",
        "an-empty-change-marker",
        "a-field-the-writer-never-writes",
        "a-scope-that-is-not-a-list",
        "a-scope-item-that-is-not-text",
        "a-scope-not-sorted",
        "a-scope-with-a-duplicate",
    ],
)
def test_bytes_this_module_never_wrote_are_not_that_document(document: bytes) -> None:
    """Every field is read back through the contract that wrote it, or not at all."""

    assert read_work_item_order_document(document) is None
