"""One tracker item as it stood at one read: exact bytes, their digest, provenance.

ADR 0010 §5 already owns how a platform object becomes something a run can
reproduce: the exact UTF-8 bytes the platform served as the object's body,
hashed as they are with nothing appended, carrying the object identity and the
read's change marker as provenance. That rule was written for the requirement
issue, and nothing in it is specific to one kind of item -- so this module is
that same rule generalised to whatever work item the connected tracker holds,
an issue or a change request, rather than a second snapshot idea beside
REQ-QUEUE-14's reference-only orchestration state.

The kinds are the neutral pair: a GitHub pull request and a GitLab merge
request are both `change_request`, and no platform word enters here. Which
spelling of a reference addresses which item stays the adapter's, exactly as
`TrackerItemReference` already says.

What stays out is as decided as what is here. Title, state, discussion, diff,
and linked items are either unbounded or platform-shaped, and none has a caller
in this slice; each joins with the caller that needs it, and the unbounded ones
arrive as artifacts addressed by hash rather than as a second byte budget
inside an order value (ADR 0010 decision 1, 2026-08-26 amendment).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from atelier2.contracts.hashing import SHA256_HEX_DIGEST, Sha256Hash
from atelier2.contracts.queue_projection import (
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    TrackerItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.schemas_v3 import SUPPORTED_DIALECT
from atelier2.contracts.when import RECORDED_AT_PATTERN, RecordedAt

MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS = 1_024


class WorkItemKind(StrEnum):
    """What a tracker item is, in words every tracker can be read into."""

    ISSUE = "issue"
    CHANGE_REQUEST = "change_request"


@dataclass(frozen=True)
class WorkItemChangeMarker:
    """The platform's own marker for the state this read saw.

    An entity tag or an update cursor, opaque here: a later read hands it back
    to ask whether anything changed (ADR 0010 §4), and a revision carries it so
    a reader can tell two reads of the same bytes apart from one read repeated.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a work item change marker must be text")
        if not 1 <= len(self.value) <= MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS:
            raise ValueError(
                "a work item change marker must contain 1 to "
                f"{MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS} characters"
            )


@dataclass(frozen=True)
class ObservedWorkItemRevision:
    """The bytes one tracker item served at one read, and what identifies them.

    The digest is derived rather than accepted, so no caller can hand in an
    identity these bytes do not have.
    """

    item: TrackerItemReference
    kind: WorkItemKind
    body: bytes
    change_marker: WorkItemChangeMarker
    observed_at: RecordedAt
    digest: Sha256Hash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.item, TrackerItemReference):
            raise TypeError(
                "an observed work item revision names its item through the contract"
            )
        if not isinstance(self.kind, WorkItemKind):
            raise TypeError("an observed work item revision carries a typed kind")
        if not isinstance(self.body, bytes):
            raise TypeError(
                "an observed work item revision carries the exact served bytes"
            )
        try:
            self.body.decode("utf-8")
        except UnicodeDecodeError:
            # ADR 0010 §5's canonical rule is about the UTF-8 bytes a platform
            # served, and a run reads them back as text; bytes that are not
            # text are not a revision this contract can carry.
            raise ValueError(
                "an observed work item revision carries UTF-8 body bytes"
            ) from None
        if not isinstance(self.change_marker, WorkItemChangeMarker):
            raise TypeError(
                "an observed work item revision carries its change marker "
                "through the contract"
            )
        if not isinstance(self.observed_at, RecordedAt):
            raise TypeError(
                "an observed work item revision carries its read time through "
                "the contract"
            )
        # Unframed on purpose: ADR 0010 §5 requires a digest a reader
        # re-derives from the object alone, which a framed preimage would make
        # Atelier-only.
        object.__setattr__(self, "digest", Sha256Hash.of(self.body))


_SCOPE_SECTION_HEADING = "## Bereich"
_GLOB_CHARACTERS = frozenset("*?[]")


class WorkItemScopeMalformed(ValueError):
    """A scope-list line that is not a repository-relative path."""

    def __init__(self, token: str) -> None:
        super().__init__(f"work item scope token {token!r} is not a relative path")
        self.token = token


def _canonical_scope_path(token: str) -> str:
    normalized = token.rstrip("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(character.isspace() for character in token)
        or any(character in _GLOB_CHARACTERS for character in token)
        or ".." in normalized.split("/")
    ):
        raise WorkItemScopeMalformed(token)
    return normalized


def _scope_section(body_text: str) -> str | None:
    lines = body_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != _SCOPE_SECTION_HEADING:
            continue
        section_lines: list[str] = []
        for later_line in lines[index + 1 :]:
            if later_line.startswith("## "):
                break
            section_lines.append(later_line)
        return "\n".join(section_lines)
    return None


@dataclass(frozen=True)
class WorkItemScope:
    """The repository-relative paths one work item body names under `## Bereich`.

    Canonically sorted and duplicate-free, so two reads of the same section
    always compare equal and a later owner (a claim, a push fence) can trust
    the tuple as it stands rather than re-deriving it from prose.
    """

    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.paths, tuple):
            raise TypeError("a work item scope holds its paths as a tuple")
        canonical = tuple(sorted({_canonical_scope_path(path) for path in self.paths}))
        if canonical != self.paths:
            raise ValueError(
                "a work item scope holds sorted, duplicate-free, normalized paths"
            )

    @classmethod
    def from_body(cls, body: bytes) -> WorkItemScope:
        """The scope one work item body declares, read exactly once.

        Each non-empty line under `## Bereich` is one repository-relative path;
        a line that is not a relative path is a named error, not a silent
        exclusion. No `## Bereich` section, or one without a path, is a valid
        empty scope. Prose elsewhere, including under `## Dateien`, is not read.
        """

        section = _scope_section(body.decode("utf-8"))
        if section is None:
            return cls(())
        tokens = [line.strip() for line in section.splitlines() if line.strip()]
        return cls(tuple(sorted({_canonical_scope_path(token) for token in tokens})))


def work_item_order_document(revision: ObservedWorkItemRevision) -> bytes:
    """The exact bytes one work-item order carries into the run that reads it.

    A run's order is material, and material is bytes: this is the one
    serialization of an observed revision, so the value a run stores, the value
    an agent reads, and the value `WORK_ITEM_ORDER_SCHEMA_DOCUMENT` describes
    are the same thing. Keys are sorted and separators are tight, so the same
    read always produces the same bytes and therefore the same value hash.
    """

    return json.dumps(
        {
            "body": revision.body.decode("utf-8"),
            "change_marker": revision.change_marker.value,
            "digest": revision.digest.value,
            "kind": revision.kind.value,
            "observed_at": revision.observed_at.value,
            "reference": revision.item.value,
            "scope": list(WorkItemScope.from_body(revision.body).paths),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


_WORK_ITEM_ORDER_SCHEMA: Final = {
    "$schema": SUPPORTED_DIALECT,
    "title": "work item",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "body",
        "change_marker",
        "digest",
        "kind",
        "observed_at",
        "reference",
        "scope",
    ],
    "properties": {
        "body": {"type": "string"},
        "change_marker": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS,
        },
        "digest": {"type": "string", "pattern": f"^{SHA256_HEX_DIGEST.pattern}$"},
        "kind": {"type": "string", "enum": [kind.value for kind in WorkItemKind]},
        "observed_at": {"type": "string", "pattern": RECORDED_AT_PATTERN},
        "reference": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
        },
        "scope": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
    },
}

WORK_ITEM_ORDER_SCHEMA_DOCUMENT: Final = json.dumps(
    _WORK_ITEM_ORDER_SCHEMA, sort_keys=True, separators=(",", ":")
).encode("utf-8")
"""The schema a workflow pins to declare an order as a tracker work item.

A workflow author does not invent a shape for what the adapter reads: they pin
this document's published revision and get the neutral kinds, the digest and
the change marker with it. Restricting a workflow to one kind is that author's
own schema built on this one, not a second grammar in the document.
"""

WORK_ITEM_ORDER_SCHEMA_REVISION: Final = PublishedRevisionHash.of(
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT
)
"""The published identity of that document, which a graph input must pin.

A start refuses to store a work item under any other schema: without this pin a
document could declare a permissive shape and a run would carry a "work item"
nothing checked. It is derived from the bytes rather than written down, so the
two can never drift apart.
"""


_WORK_ITEM_ORDER_FIELDS: Final = frozenset(
    _WORK_ITEM_ORDER_SCHEMA["required"]  # type: ignore[arg-type]
)


@dataclass(frozen=True)
class WorkItemOrderDocument:
    """One work-item order value, read back as the typed fields it was written from.

    Typed, because a reader that answered strings would let a caller decide what
    "the item this order names" is: a reference longer than one can be, an
    instant no calendar has. Reading it back through the same contracts that
    wrote it is what makes the answer worth comparing runs by.
    """

    body: str
    change_marker: WorkItemChangeMarker
    digest: Sha256Hash
    kind: WorkItemKind
    observed_at: RecordedAt
    reference: TrackerItemReference
    scope: WorkItemScope


def read_work_item_order_document(document: bytes) -> WorkItemOrderDocument | None:
    """These bytes as the complete document `work_item_order_document` writes.

    Complete is the point, not merely parseable: every field this module writes
    is present, no other is, each is the value type that wrote it, and the
    digest is the one those body bytes have. A reader that accepted less would
    let a value under the work-item schema mean something no read produced --
    which is exactly what a stored order under that schema is not allowed to be.

    `None` says these bytes are not that document. Whether that is a caller's
    material or durable state that lies is the caller's judgement, and the two
    answers differ: one refuses a start, the other says the store is corrupt.
    """

    try:
        value = json.loads(document)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != _WORK_ITEM_ORDER_FIELDS:
        return None
    scope_value = value.get("scope")
    if not isinstance(scope_value, list) or not all(
        isinstance(path, str) for path in scope_value
    ):
        return None
    if not all(
        isinstance(field_value, str)
        for name, field_value in value.items()
        if name != "scope"
    ):
        return None
    try:
        read = WorkItemOrderDocument(
            value["body"],
            WorkItemChangeMarker(value["change_marker"]),
            Sha256Hash(value["digest"]),
            WorkItemKind(value["kind"]),
            RecordedAt(value["observed_at"]),
            TrackerItemReference(value["reference"]),
            WorkItemScope(tuple(scope_value)),
        )
    except ValueError:
        # Every field is read back through the contract that wrote it, so a
        # reference too long to be one or an instant no calendar has answers
        # here rather than travelling on as a fact.
        return None
    if read.digest != Sha256Hash.of(read.body.encode("utf-8")):
        return None
    return read
