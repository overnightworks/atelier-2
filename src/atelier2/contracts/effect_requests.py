"""Canonical request and result bytes for the two published effect operations."""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, Self

from atelier2.contracts.effect_markers import commit_message

if TYPE_CHECKING:
    from atelier2.contracts.queue_projection import TrackerItemReference

_SAFE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_UNSAFE_BRANCH_FRAGMENTS = ("..", "@{", "//")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _object(value: bytes, owner: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{owner} is not canonical JSON") from error
    if not isinstance(decoded, dict) or _canonical_json(decoded) != value:
        raise ValueError(f"{owner} is not one canonical JSON object")
    return decoded


def _fields(value: dict[str, Any], expected: frozenset[str], owner: str) -> None:
    if frozenset(value) != expected:
        raise ValueError(f"{owner} carries exactly {', '.join(sorted(expected))}")


def _tracker_item_reference_type() -> type[TrackerItemReference]:
    from atelier2.contracts.queue_projection import TrackerItemReference

    return TrackerItemReference


def _tracker_item_reference(value: str) -> TrackerItemReference:
    return _tracker_item_reference_type()(value)


@dataclass(frozen=True, slots=True)
class GitCommitIdentity:
    name: str
    email: str

    def __post_init__(self) -> None:
        if not self.name or any(character in self.name for character in "\r\n<>"):
            raise ValueError("a git identity name is nonempty and header-safe")
        if (
            not self.email
            or "@" not in self.email
            or any(character in self.email for character in "\r\n<> ")
        ):
            raise ValueError("a git identity email is nonempty and header-safe")

    def as_json(self) -> dict[str, str]:
        return {"email": self.email, "name": self.name}

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) != {"name", "email"}:
            raise ValueError("a git identity carries name and email")
        name = value["name"]
        email = value["email"]
        if not isinstance(name, str) or not isinstance(email, str):
            raise TypeError("a git identity name and email are text")
        return cls(name, email)


@dataclass(frozen=True, slots=True)
class HeadBranch:
    value: str

    def __post_init__(self) -> None:
        unsafe = (
            not self.value
            or len(self.value) > 240
            or _SAFE_BRANCH.fullmatch(self.value) is None
            or any(fragment in self.value for fragment in _UNSAFE_BRANCH_FRAGMENTS)
            or self.value.endswith(("/", ".", ".lock"))
            or any(
                part.startswith(".") or part.endswith(".lock")
                for part in self.value.split("/")
            )
        )
        if unsafe:
            raise ValueError(f"unsafe branch {self.value!r}")

    @property
    def full_ref(self) -> str:
        return f"refs/heads/{self.value}"


class QueueItemIdentity(Protocol):
    @property
    def value(self) -> str: ...


def head_branch_for_queue_item(item_id: QueueItemIdentity) -> HeadBranch:
    return HeadBranch(f"atelier2/work-item/{item_id.value}")


def head_branch_for_unbound_request(payload: bytes) -> HeadBranch:
    return HeadBranch(f"atelier2-open-pr-{hashlib.sha256(payload).hexdigest()[:12]}")


@dataclass(frozen=True, slots=True)
class OpenPullRequest:
    body: str
    head_branch: HeadBranch
    work_item_reference: TrackerItemReference | None = None

    def __post_init__(self) -> None:
        if self.work_item_reference is not None and not isinstance(
            self.work_item_reference, _tracker_item_reference_type()
        ):
            raise TypeError("an open-pr work item reference uses the tracker contract")

    def canonical_bytes(self) -> bytes:
        # An absent reference's canonical bytes and hash are durable identity:
        # in-flight intents opened before #1290 are reconciled by this exact
        # form, so the key is omitted rather than carried as a `null`.
        value: dict[str, str] = {
            "body": self.body,
            "head_branch": self.head_branch.value,
        }
        if self.work_item_reference is not None:
            value["work_item_reference"] = self.work_item_reference.value
        return _canonical_json(value)

    @classmethod
    def from_canonical_bytes(cls, request: bytes) -> Self:
        value = _object(request, "open-pr request")
        fields = frozenset(value)
        legacy_fields = frozenset(("body", "head_branch"))
        current_fields = legacy_fields | {"work_item_reference"}
        if fields not in (legacy_fields, current_fields):
            raise ValueError("open-pr request carries its declared fields")
        body = value["body"]
        branch = value["head_branch"]
        if not isinstance(body, str) or not isinstance(branch, str):
            raise TypeError("open-pr body and head_branch are text")
        if fields == legacy_fields:
            return cls(body, HeadBranch(branch))
        reference = value["work_item_reference"]
        if not isinstance(reference, str):
            raise TypeError("open-pr work_item_reference is text")
        return cls(body, HeadBranch(branch), _tracker_item_reference(reference))


@dataclass(frozen=True, slots=True)
class ReviewedDocumentReplacement:
    """One exact file replacement an independently reviewed release may publish."""

    path: str
    current_digest: str
    replacement: bytes

    def __post_init__(self) -> None:
        if (
            not self.path
            or self.path.startswith("/")
            or ".." in self.path.split("/")
            or "\\" in self.path
        ):
            raise ValueError("a reviewed replacement path stays inside the repository")
        if len(self.current_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.current_digest
        ):
            raise ValueError("a reviewed replacement has a SHA-256 current digest")
        try:
            self.replacement.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("a reviewed replacement is UTF-8") from error

    def as_json(self) -> dict[str, str]:
        return {
            "current_digest": self.current_digest,
            "path": self.path,
            "replacement_base64": b64encode(self.replacement).decode("ascii"),
        }

    def as_candidate_json(self) -> dict[str, str]:
        return {
            "current_digest": self.current_digest,
            "path": self.path,
            "replacement_utf8_content": self.replacement.decode("utf-8"),
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise TypeError("a reviewed replacement is an object")
        _fields(
            value,
            frozenset(("current_digest", "path", "replacement_base64")),
            "reviewed replacement",
        )
        path = value["path"]
        current_digest = value["current_digest"]
        replacement = value["replacement_base64"]
        if not all(
            isinstance(field, str) for field in (path, current_digest, replacement)
        ):
            raise TypeError("a reviewed replacement carries text fields")
        try:
            replacement_bytes = b64decode(replacement, validate=True)
        except ValueError as error:
            raise ValueError("a reviewed replacement is base64") from error
        return cls(path, current_digest, replacement_bytes)


def reviewed_documentation_candidate_digest(
    base_revision: str,
    replacements: tuple[ReviewedDocumentReplacement, ...],
    title: str,
    body: str,
) -> str:
    """Digest the exact release candidate fields other than its digest slot."""

    return hashlib.sha256(
        _canonical_json(
            {
                "base_revision": base_revision,
                "body": body,
                "changes": [entry.as_candidate_json() for entry in replacements],
                "title": title,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewedDocumentationPullRequest:
    """The versioned, closed request for an approved documentation release."""

    base_revision: str
    candidate_digest: str
    reviewed_verdict_digest: str
    replacements: tuple[ReviewedDocumentReplacement, ...]
    title: str
    body: str
    head_branch: HeadBranch
    draft: bool = True

    def __post_init__(self) -> None:
        if len(self.base_revision) not in (40, 64) or any(
            character not in "0123456789abcdef" for character in self.base_revision
        ):
            raise ValueError("a documentation release pins its base revision")
        for digest, owner in (
            (self.candidate_digest, "candidate"),
            (self.reviewed_verdict_digest, "reviewed verdict"),
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(f"a documentation release pins its {owner} digest")
        if not self.replacements or len(
            {entry.path for entry in self.replacements}
        ) != len(self.replacements):
            raise ValueError("a documentation release replaces each file exactly once")
        if not self.title or not self.body or not self.draft:
            raise ValueError(
                "a documentation release has a title, body, and draft flag"
            )

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "base_revision": self.base_revision,
                "body": self.body,
                "candidate_digest": self.candidate_digest,
                "draft": True,
                "head_branch": self.head_branch.value,
                "replacement_files": [entry.as_json() for entry in self.replacements],
                "reviewed_verdict_digest": self.reviewed_verdict_digest,
                "title": self.title,
                "version": 2,
            }
        )

    @classmethod
    def from_canonical_bytes(cls, request: bytes) -> Self:
        value = _object(request, "reviewed documentation open-pr request")
        _fields(
            value,
            frozenset(
                (
                    "base_revision",
                    "body",
                    "candidate_digest",
                    "draft",
                    "head_branch",
                    "replacement_files",
                    "reviewed_verdict_digest",
                    "title",
                    "version",
                )
            ),
            "reviewed documentation open-pr request",
        )
        if value["version"] != 2 or value["draft"] is not True:
            raise ValueError("a documentation release is version 2 and draft")
        text_names = (
            "base_revision",
            "body",
            "candidate_digest",
            "head_branch",
            "reviewed_verdict_digest",
            "title",
        )
        if any(
            not isinstance(value[name], str) for name in text_names
        ) or not isinstance(value["replacement_files"], list):
            raise TypeError("a documentation release carries its closed request fields")
        return cls(
            value["base_revision"],
            value["candidate_digest"],
            value["reviewed_verdict_digest"],
            tuple(
                ReviewedDocumentReplacement.from_json(entry)
                for entry in value["replacement_files"]
            ),
            value["title"],
            value["body"],
            HeadBranch(value["head_branch"]),
            True,
        )


@dataclass(frozen=True, slots=True)
class PushAtelierCommit:
    attempt_id: str
    candidate_tree: str
    base_commit: str
    head_branch: HeadBranch
    author: GitCommitIdentity
    committer: GitCommitIdentity
    completed_at: str

    def __post_init__(self) -> None:
        if len(self.attempt_id) != 64 or any(
            c not in "0123456789abcdef" for c in self.attempt_id
        ):
            raise ValueError("a push request attempt id is a SHA-256 hash")
        lengths = {len(self.candidate_tree), len(self.base_commit)}
        if lengths not in ({40}, {64}) or any(
            any(character not in "0123456789abcdef" for character in value)
            for value in (self.candidate_tree, self.base_commit)
        ):
            raise ValueError("a push request base and tree use one git object format")
        try:
            datetime.strptime(self.completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError as error:
            raise ValueError("a push completion timestamp is RFC 3339 UTC") from error

    @property
    def object_format(self) -> str:
        return "sha1" if len(self.base_commit) == 40 else "sha256"

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "attempt_id": self.attempt_id,
                "author": self.author.as_json(),
                "base_commit": self.base_commit,
                "candidate_tree": self.candidate_tree,
                "committer": self.committer.as_json(),
                "completed_at": self.completed_at,
                "head_branch": self.head_branch.value,
            }
        )

    @classmethod
    def from_canonical_bytes(cls, request: bytes) -> Self:
        value = _object(request, "push request")
        _fields(
            value,
            frozenset(
                (
                    "attempt_id",
                    "author",
                    "base_commit",
                    "candidate_tree",
                    "committer",
                    "completed_at",
                    "head_branch",
                )
            ),
            "push request",
        )
        text_fields = (
            "attempt_id",
            "base_commit",
            "candidate_tree",
            "completed_at",
            "head_branch",
        )
        if any(not isinstance(value[field], str) for field in text_fields):
            raise ValueError("push request identity, objects, time and branch are text")
        return cls(
            value["attempt_id"],
            value["candidate_tree"],
            value["base_commit"],
            HeadBranch(value["head_branch"]),
            GitCommitIdentity.from_json(value["author"]),
            GitCommitIdentity.from_json(value["committer"]),
            value["completed_at"],
        )

    def commit_bytes(self, request_hash: str) -> bytes:
        completed = datetime.strptime(self.completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        timestamp = int(completed.timestamp())
        lines = (
            f"tree {self.candidate_tree}\n"
            f"parent {self.base_commit}\n"
            f"author {self.author.name} <{self.author.email}> {timestamp} +0000\n"
            f"committer {self.committer.name} <{self.committer.email}> {timestamp} +0000\n"
            "\n"
            f"{commit_message(self.attempt_id, request_hash)}"
        )
        return lines.encode("utf-8")

    def expected_commit_oid(self, request_hash: str) -> str:
        content = self.commit_bytes(request_hash)
        object_bytes = f"commit {len(content)}\0".encode("ascii") + content
        algorithm = hashlib.sha1 if self.object_format == "sha1" else hashlib.sha256
        return algorithm(object_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class PushAtelierCommitReceipt:
    remote_identity: str
    full_ref: str
    commit_oid: str
    parent: str
    candidate_tree: str
    branch: str
    author: GitCommitIdentity
    committer: GitCommitIdentity

    @classmethod
    def from_result_bytes(cls, result: bytes) -> Self:
        value = _object(result, "push receipt")
        _fields(
            value,
            frozenset(
                (
                    "author",
                    "branch",
                    "candidate_tree",
                    "commit_oid",
                    "committer",
                    "full_ref",
                    "parent",
                    "remote_identity",
                )
            ),
            "push receipt",
        )
        text_fields = (
            "branch",
            "candidate_tree",
            "commit_oid",
            "full_ref",
            "parent",
            "remote_identity",
        )
        if any(not isinstance(value[field], str) for field in text_fields):
            raise TypeError("push receipt identity, objects and branch are text")
        return cls(
            value["remote_identity"],
            value["full_ref"],
            value["commit_oid"],
            value["parent"],
            value["candidate_tree"],
            value["branch"],
            GitCommitIdentity.from_json(value["author"]),
            GitCommitIdentity.from_json(value["committer"]),
        )

    def result_bytes(self) -> bytes:
        return _canonical_json(
            {
                "author": self.author.as_json(),
                "branch": self.branch,
                "candidate_tree": self.candidate_tree,
                "commit_oid": self.commit_oid,
                "committer": self.committer.as_json(),
                "full_ref": self.full_ref,
                "parent": self.parent,
                "remote_identity": self.remote_identity,
            }
        )
