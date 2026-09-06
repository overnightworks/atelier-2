"""What a document published as an `adapter_operation` revision names.

ADR 0006 makes an Action node's `operation` a versioned reference into the
`adapter_operation` registry, so a platform effect is never a mark an author
writes: it is a published revision the document pins by hash, exactly like the
schema of an output. Bytes published under that kind are an operation only
because someone called them one, so this module is the reading that turns them
into one -- a pure function over bytes, asked once by the publication door and
again by the reference resolution that binds a run.

The vocabulary is closed. A document may declare only the two operations a
graph performs on its author's word; `claim-work-item` is the runtime's own
precondition and belongs to no document, so declaring it is refused here just
like a word no runtime performs. A document naming anything else is refused
where it was declared rather than resolved and then silently unperformed,
because a run started under an operation nothing performs would tell its author
that the atelier did what the document asked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.effect_requests import GitCommitIdentity

MAXIMUM_ADAPTER_OPERATION_DOCUMENT_BYTES = 4_096
"""How large an adapter-operation document may be. It names one operation."""


class AdapterOperationName(StrEnum):
    """The closed set of operations this runtime performs.

    An operation enters here together with the adapter that performs it, so the
    set and the performance never disagree. Which of them a published revision
    may name is the narrower question `_DECLARABLE_OPERATIONS` answers.
    """

    OPEN_PR = "open-pr"
    PUSH_ATELIER_COMMIT = "push-atelier-commit"
    CLAIM_WORK_ITEM = "claim-work-item"


class AdapterOperationRefusal(StrEnum):
    """Why published bytes are not an adapter operation, as a stable token."""

    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_NOT_UTF8 = "document_not_utf8"
    NOT_AN_OPERATION_OBJECT = "not_an_operation_object"
    MISSING_OPERATION = "missing_operation"
    UNKNOWN_OPERATION = "unknown_operation"
    UNKNOWN_FIELD = "unknown_field"


@dataclass(frozen=True, slots=True)
class AdapterOperationAccepted:
    """These bytes name exactly this adapter operation."""

    operation: AdapterOperationName
    author: GitCommitIdentity | None = None
    committer: GitCommitIdentity | None = None


@dataclass(frozen=True, slots=True)
class AdapterOperationRefused:
    """These bytes are not an operation this runtime performs, and why."""

    reason: AdapterOperationRefusal
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.reason.value}{suffix}"


type AdapterOperationVerdict = AdapterOperationAccepted | AdapterOperationRefused

_OPERATION_FIELD = "operation"
_IDENTITY_FIELDS = frozenset(("author", "committer"))
_DECLARABLE_OPERATIONS = frozenset(
    operation.value
    for operation in (
        AdapterOperationName.OPEN_PR,
        AdapterOperationName.PUSH_ATELIER_COMMIT,
    )
)
"""Which operations a document may name. `claim-work-item` is the runtime's own
precondition, taken by the node workflow rather than declared by an author."""


def read_adapter_operation_document(document: bytes) -> AdapterOperationVerdict:
    """Whether these exact published bytes name an operation this runtime performs."""
    if len(document) > MAXIMUM_ADAPTER_OPERATION_DOCUMENT_BYTES:
        return AdapterOperationRefused(
            AdapterOperationRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_ADAPTER_OPERATION_DOCUMENT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return AdapterOperationRefused(
            AdapterOperationRefusal.DOCUMENT_NOT_UTF8, broken.reason
        )
    try:
        decoded = json.loads(text)
    except ValueError as broken:
        return AdapterOperationRefused(
            AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT, str(broken)
        )
    if not isinstance(decoded, dict):
        return AdapterOperationRefused(
            AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT,
            f"an adapter operation is an object, not {type(decoded).__name__}",
        )
    named = decoded.get(_OPERATION_FIELD)
    if not isinstance(named, str):
        return AdapterOperationRefused(
            AdapterOperationRefusal.MISSING_OPERATION,
            f"an adapter operation names the operation it performs under "
            f"{_OPERATION_FIELD}",
        )
    if named not in _DECLARABLE_OPERATIONS:
        return AdapterOperationRefused(
            AdapterOperationRefusal.UNKNOWN_OPERATION,
            f"no document here declares {named!r}",
        )
    operation = AdapterOperationName(named)
    allowed = (
        frozenset((_OPERATION_FIELD, *_IDENTITY_FIELDS))
        if operation is AdapterOperationName.PUSH_ATELIER_COMMIT
        else frozenset((_OPERATION_FIELD,))
    )
    unknown = sorted(set(decoded) - allowed)
    if unknown:
        return AdapterOperationRefused(
            AdapterOperationRefusal.UNKNOWN_FIELD,
            f"{operation.value} does not declare {', '.join(unknown)}",
        )
    if set(decoded) != allowed:
        return AdapterOperationRefused(
            AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT,
            "push-atelier-commit declares author and committer identities",
        )
    if operation is not AdapterOperationName.PUSH_ATELIER_COMMIT:
        return AdapterOperationAccepted(operation)
    try:
        author = GitCommitIdentity.from_json(decoded["author"])
        committer = GitCommitIdentity.from_json(decoded["committer"])
    except (TypeError, ValueError) as error:
        return AdapterOperationRefused(
            AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT, str(error)
        )
    return AdapterOperationAccepted(operation, author, committer)
