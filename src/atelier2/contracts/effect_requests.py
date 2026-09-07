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
from atelier2.contracts.hashing import frame
from atelier2.contracts.runs import RunId

if TYPE_CHECKING:
    from atelier2.contracts.queue_projection import TrackerItemReference

_SAFE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
# aco accepts exactly this claim id shape (its `protocol.CLAIM_ID_PATTERN`),
# so a request it would reject never reaches it.
_CLAIM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
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
        reference = value.get("work_item_reference")
        if reference is None:
            return cls(body, HeadBranch(branch))
        if not isinstance(reference, str):
            raise TypeError("open-pr work_item_reference is text")
        return cls(body, HeadBranch(branch), _tracker_item_reference(reference))


@dataclass(frozen=True, slots=True)
class ClaimReasons:
    """Why the ledger may waive its two checks for this claim, in the run's words.

    `whole` answers the width check: it is always given, because the scope is
    the item body's own cut and the ledger ignores the sentence where nothing
    trips. `out_of_order` answers the board-order check and exists only where
    an operator's admission already decided the order; a run without one is
    refused by priority as a person would be.
    """

    whole: str
    out_of_order: str | None

    def __post_init__(self) -> None:
        if not self.whole:
            raise ValueError("a claim always says why its scope does not split")
        if self.out_of_order == "":
            raise ValueError("an out-of-order reason is a sentence or absent")

    def as_json(self) -> dict[str, str | None]:
        return {"out_of_order": self.out_of_order, "whole": self.whole}

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise TypeError("claim reasons are an object")
        _fields(value, frozenset(("out_of_order", "whole")), "claim reasons")
        whole = value["whole"]
        out_of_order = value["out_of_order"]
        if not isinstance(whole, str):
            raise TypeError("a claim's whole-scope reason is text")
        if out_of_order is not None and not isinstance(out_of_order, str):
            raise TypeError("a claim's out-of-order reason is text or absent")
        return cls(whole, out_of_order)


@dataclass(frozen=True, slots=True)
class ClaimWorkItem:
    """The lane claim one run holds on its work item before it edits anything.

    The claim id is minted by `work_item_claim_id` from the run and the item,
    so a retry asks the ledger about the same claim instead of taking a second
    one, and the ledger answers under an identity this runtime can read back.
    The agent identity is not restated here: the intent's own binding names the
    run, and the claim boundary renders `atelier2 run <run-id>` from it. The
    reasons travel in the canonical bytes so a replay sends the ledger exactly
    what was prepared, never a sentence composed afresh from later state.
    """

    item: int
    claim_id: str
    head_branch: HeadBranch
    scope: tuple[str, ...]
    reasons: ClaimReasons

    def __post_init__(self) -> None:
        if self.item <= 0:
            raise ValueError("a claim names its work item by positive number")
        if _CLAIM_ID.fullmatch(self.claim_id) is None:
            raise ValueError(f"unsafe claim id {self.claim_id!r}")
        if not self.scope or tuple(sorted(set(self.scope))) != self.scope:
            raise ValueError("a claim scope is nonempty, sorted and duplicate-free")

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "claim_id": self.claim_id,
                "head_branch": self.head_branch.value,
                "item": self.item,
                "reasons": self.reasons.as_json(),
                "scope": list(self.scope),
            }
        )

    @classmethod
    def from_canonical_bytes(cls, request: bytes) -> Self:
        value = _object(request, "claim-work-item request")
        _fields(
            value,
            frozenset(("claim_id", "head_branch", "item", "reasons", "scope")),
            "claim-work-item request",
        )
        item = value["item"]
        scope = value["scope"]
        if type(item) is not int or not isinstance(scope, list):
            raise TypeError("a claim request carries an item number and a scope list")
        if any(not isinstance(path, str) for path in scope):
            raise TypeError("a claim request scope is text")
        if not isinstance(value["claim_id"], str):
            raise TypeError("a claim request claim id is text")
        return cls(
            item,
            value["claim_id"],
            HeadBranch(value["head_branch"]),
            tuple(scope),
            ClaimReasons.from_json(value["reasons"]),
        )


@dataclass(frozen=True, slots=True)
class ClaimedLanePath:
    """One foreign live claim whose scope touches the claim just acquired."""

    claim_id: str
    agent: str
    scope: tuple[str, ...]
    item: int | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "claim_id": self.claim_id,
            "item": self.item,
            "scope": list(self.scope),
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise TypeError("a touched lane is an object")
        _fields(
            value, frozenset(("agent", "claim_id", "item", "scope")), "touched lane"
        )
        item = value["item"]
        scope = value["scope"]
        if item is not None and type(item) is not int:
            raise TypeError("a touched lane names its item by number or not at all")
        if not isinstance(scope, list) or any(
            not isinstance(path, str) for path in scope
        ):
            raise TypeError("a touched lane scope is text")
        if any(not isinstance(value[name], str) for name in ("agent", "claim_id")):
            raise TypeError("a touched lane agent and claim id are text")
        return cls(value["claim_id"], value["agent"], tuple(scope), item)


@dataclass(frozen=True, slots=True)
class ClaimWorkItemReceipt:
    """What the claim ledger confirmed for one exact claim request.

    `claimed_scope` is the ledger's own answer about this run's paths, and
    `touches` are the foreign lanes it found standing on them -- never this
    run's own scope, which is why the two are separate fields rather than one
    list a reader would have to take apart.
    """

    item: int
    claim_id: str
    agent: str
    branch: HeadBranch
    claimed_scope: tuple[str, ...]
    touches: tuple[ClaimedLanePath, ...] = ()

    def result_bytes(self) -> bytes:
        return _canonical_json(
            {
                "agent": self.agent,
                "branch": self.branch.value,
                "claim_id": self.claim_id,
                "claimed_scope": list(self.claimed_scope),
                "item": self.item,
                "touches": [touch.as_json() for touch in self.touches],
            }
        )

    @classmethod
    def from_result_bytes(cls, result: bytes) -> Self:
        value = _object(result, "claim-work-item receipt")
        _fields(
            value,
            frozenset(
                ("agent", "branch", "claim_id", "claimed_scope", "item", "touches")
            ),
            "claim-work-item receipt",
        )
        item = value["item"]
        claimed_scope = value["claimed_scope"]
        touches = value["touches"]
        if type(item) is not int or not isinstance(claimed_scope, list):
            raise TypeError("a claim receipt carries an item number and its scope")
        if any(not isinstance(path, str) for path in claimed_scope):
            raise TypeError("a claim receipt scope is text")
        if not isinstance(touches, list):
            raise TypeError("a claim receipt carries its touched lanes as a list")
        if any(not isinstance(value[name], str) for name in ("agent", "claim_id")):
            raise TypeError("a claim receipt agent and claim id are text")
        return cls(
            item,
            value["claim_id"],
            value["agent"],
            HeadBranch(value["branch"]),
            tuple(claimed_scope),
            tuple(ClaimedLanePath.from_json(touch) for touch in touches),
        )


def work_item_claim_id(run_id: RunId, item: int) -> str:
    """The one claim id this run takes on this item, however often it retries."""

    digest = hashlib.sha256(
        frame(
            "work-item-claim-id/v1",
            run_id.value.encode("utf-8"),
            str(item).encode("ascii"),
        )
    ).hexdigest()
    return f"atelier2-{digest}"


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
