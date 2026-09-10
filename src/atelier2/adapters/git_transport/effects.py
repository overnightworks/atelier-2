"""Create-only publication of one deterministic Atelier candidate commit."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, assert_never

from atelier2.adapters.project_source import isolated_git_environment
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    GitCommitIdentity,
    PushAtelierCommit,
    PushAtelierCommitReceipt,
    ReviewedDocumentationPullRequest,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectAdapterBinding,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentMismatch,
    EffectReceipt,
    EffectResult,
    EffectUnknownOutcome,
    LogicalEffectKey,
    PerformedEffect,
    ReadbackPhase,
    UnknownOutcomeReason,
    destination_holds_nothing,
)
from atelier2.ports.effects import (
    HeadBranchPullRequests,
    HeadBranchPullRequestsUnreadable,
    NoPullRequestOpenOnHeadBranch,
    PullRequestOpenOnHeadBranch,
)

_HOOK_FREE_ARGUMENTS = ("-c", f"core.hooksPath={os.devnull}")
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

_LOG = logging.getLogger("atelier2")


class GitTransportRefused(RuntimeError):
    """The durable request does not authorize the proposed git mutation."""


class GitCredentialUnresolvable(GitTransportRefused):
    """The remote's credential file holds no well-formed token (`platform-credential-unresolvable`)."""


class TokenFileProblem(StrEnum):
    """Why a credential file holds no token a request may carry, never quoting it."""

    MISSING = "does not exist"
    UNREADABLE = "is not readable"
    EMPTY = "is empty"
    MALFORMED = "does not hold exactly one token of visible ASCII characters"


_VISIBLE_ASCII = range(0x21, 0x7F)


def read_token_file(path: Path) -> str | TokenFileProblem:
    """The one token a credential file holds, or why it holds none.

    The owner of what a token file may hold, for every adapter that sends one:
    visible ASCII once the edges are trimmed, since anything else can reach an
    HTTP header or a git prompt where a protocol error or a log prints it.
    """

    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return TokenFileProblem.MISSING
    except OSError:
        return TokenFileProblem.UNREADABLE
    token = contents.strip()
    if not token:
        return TokenFileProblem.EMPTY
    if any(byte not in _VISIBLE_ASCII for byte in token):
        return TokenFileProblem.MALFORMED
    return token.decode("ascii")


def _require_a_readable_token(credential_file: Path | None) -> None:
    """Refuse a remote call whose credential helper would not answer git with one token.

    Checked per remote call, so a token set while the host serves counts; what
    is read here is dropped at once, since the helper git invokes reads the file.
    """

    if credential_file is None:
        return
    token = read_token_file(credential_file)
    if isinstance(token, TokenFileProblem):
        raise GitCredentialUnresolvable(
            f"platform-credential-unresolvable: git credential file "
            f"{credential_file} {token.value}"
        )


@dataclass(frozen=True, slots=True)
class GitRemote:
    identity: str
    url: str
    credential_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.identity or not self.url:
            raise ValueError("a git remote has a stable identity and URL")
        if self.url.startswith("-"):
            raise ValueError("a git remote URL cannot begin with an option marker")


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class _RemoteRefFound:
    """The remote advertises exactly this object at the exact ref that was read."""

    oid: str


@dataclass(frozen=True, slots=True)
class _RemoteRefAbsent:
    """The remote answered, and it holds no such ref.

    What that proves depends on when it was read, so the observation keeps what
    a reader after a send needs to report: the status the read exited with, and
    how long it took.
    """

    exit_status: int
    duration_milliseconds: int


@dataclass(frozen=True, slots=True)
class _RemoteRefUnreadable:
    """The read itself failed, or answered something this adapter cannot read."""

    reason: UnknownOutcomeReason


type _RemoteRefObservation = _RemoteRefFound | _RemoteRefAbsent | _RemoteRefUnreadable


def _elapsed_milliseconds(started: float) -> int:
    return round((time.monotonic() - started) * 1_000)


def _unreadable(
    result: GitCommandResult, elapsed_milliseconds: int
) -> _RemoteRefUnreadable:
    """Why a remote read resolved nothing, in git's own words.

    A read that failed says so on standard error. A read that exits zero and
    advertises something this adapter cannot parse says nothing there, and then
    the unparsable answer itself is the only account of it there is.
    """

    said = result.stderr.decode("utf-8", errors="replace").strip()
    return _RemoteRefUnreadable(
        UnknownOutcomeReason(
            result.returncode,
            elapsed_milliseconds,
            said or result.stdout.decode("utf-8", errors="replace").strip(),
        )
    )


def _unknown(
    intent: EffectIntent, observation: _RemoteRefObservation
) -> EffectUnknownOutcome:
    return EffectUnknownOutcome(intent.reference, _unknown_reason(observation))


def _unknown_reason(observation: _RemoteRefObservation) -> UnknownOutcomeReason | None:
    """What to report about a read whose answer resolved nothing.

    A ref the remote does not advertise is an answer, not a failure, so after a
    send it reports the read's own successful status rather than pretending git
    refused anything.
    """

    if isinstance(observation, _RemoteRefUnreadable):
        return observation.reason
    if isinstance(observation, _RemoteRefAbsent):
        return UnknownOutcomeReason(
            observation.exit_status,
            observation.duration_milliseconds,
            "the remote advertises no such ref, and a send was already attempted",
        )
    return None


def _log_branch_head_replaced(
    full_ref: str, replaced_oid: str, commit_oid: str
) -> None:
    """The one record of which oid a lease push replaced.

    The replaced oid is evidence for an operator reading why a branch moved,
    not identity: the receipt this push produces must be the same bytes a
    later crash readback of the same accepted send reconstructs, and a
    readback has no way to recover which oid a now-overwritten branch used to
    carry.
    """

    _LOG.info(
        "A lease push replaced a branch head nobody reviews any more: %s",
        full_ref,
        extra={
            "event": "git_transport_push_replaced_branch_head",
            "full_ref": full_ref,
            "replaced_oid": replaced_oid,
            "commit_oid": commit_oid,
        },
    )


class GitCommandRunner(Protocol):
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult: ...


class SubprocessGitCommandRunner:
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=working_directory,
            env=environment,
            input=standard_input,
            capture_output=True,
            check=False,
        )
        return GitCommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class GitTransportEffectAdapterFactory:
    candidate_store: Path
    remote: GitRemote
    adapter_revision: AdapterRevision
    destination: EffectDestination
    head_branch_pull_requests: HeadBranchPullRequests
    command_runner: GitCommandRunner = field(default_factory=SubprocessGitCommandRunner)

    @property
    def binding(self) -> EffectAdapterBinding:
        return EffectAdapterBinding(
            self.adapter_revision,
            self.destination,
            AdapterOperationalIdentity(self.remote.identity),
            AdapterOperationName.PUSH_ATELIER_COMMIT,
        )

    @property
    def proves_absence(self) -> bool:
        # An `ls-remote` for one exact ref that succeeds and advertises nothing
        # is the remote's own answer that it holds no such ref (#1210). Only a
        # read that failed leaves the outcome unknown.
        return True

    def open(self) -> GitTransportEffectAdapter:
        credential_file = self.remote.credential_file
        # Absolute, not resolved: a repointed symlink is followed, not pinned.
        absolute_credential_file = (
            None if credential_file is None else credential_file.absolute()
        )
        return GitTransportEffectAdapter(
            self.candidate_store.resolve(),
            GitRemote(self.remote.identity, self.remote.url, absolute_credential_file),
            self.binding,
            self.head_branch_pull_requests,
            self.command_runner,
        )


class GitTransportEffectAdapter:
    _OBJECT_NAME_FORMAT = "--format=%(objectname)"

    def __init__(
        self,
        candidate_store: Path,
        remote: GitRemote,
        binding: EffectAdapterBinding,
        head_branch_pull_requests: HeadBranchPullRequests,
        command_runner: GitCommandRunner,
    ) -> None:
        self._candidate_store = candidate_store
        self._remote = remote
        self._binding = binding
        self._head_branch_pull_requests = head_branch_pull_requests
        self._command_runner = command_runner
        self._closed = False

    def readback(
        self, intent: EffectIntent, phase: ReadbackPhase
    ) -> EffectReceipt | EffectAbsence | EffectUnknownOutcome:
        request = self._authorized_request(intent)
        self._verify_candidate(request)
        expected = request.expected_commit_oid(intent.request.request_hash.value)
        observation = self._remote_ref(request.head_branch.full_ref)
        if isinstance(observation, _RemoteRefAbsent):
            return destination_holds_nothing(
                intent.reference, phase, _unknown_reason(observation)
            )
        if not isinstance(observation, _RemoteRefFound):
            return _unknown(intent, observation)
        if observation.oid != expected:
            if phase is ReadbackPhase.AFTER_SEND:
                # A foreign commit read after a send is the divergence this
                # operation never resolves for itself: from here, a branch
                # someone else moved and one this send lost a race for look
                # the same, and no answer about its pull requests changes that.
                return _unknown(intent, observation)
            return self._replaceable_branch(intent, request)
        performed = self._performed(request, expected)
        return EffectReceipt(
            intent,
            performed.effect_id,
            performed.result,
            ConfirmationSource.ADAPTER_READBACK,
        )

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        request = self._authorized_request(intent)
        self._verify_candidate(request)
        expected = request.expected_commit_oid(intent.request.request_hash.value)
        full_ref = request.head_branch.full_ref
        # This read only re-reads a send the caller already holds a pre-send
        # absence or an operator's determination for; the create-only lease
        # below is what fences the send itself. The read taken after it is
        # `_receipt_after_send`, which never reads a missing ref as an absence.
        observation = self._remote_ref(full_ref)
        if isinstance(observation, _RemoteRefFound) and observation.oid == expected:
            return self._performed(request, expected)
        replaced_oid: str | None = None
        if isinstance(observation, _RemoteRefFound):
            licensed = self._replaceable_branch(intent, request)
            if isinstance(licensed, EffectUnknownOutcome):
                return licensed
            replaced_oid = observation.oid
        elif not isinstance(observation, _RemoteRefAbsent):
            return _unknown(intent, observation)
        self._fetch_base(request.base_commit)
        written = self._store_commit(request, intent.request.request_hash.value)
        if written != expected:
            raise GitTransportRefused(
                "git did not preserve the deterministic commit object identity"
            )
        # The lease names exactly what this attempt read: nothing where the ref
        # was absent, and the replaced commit where it stood at one. A branch
        # that moves between that read and this send fails the lease and stays
        # as it is, so no send ever overwrites a value it did not observe.
        leased = "0" * len(expected) if replaced_oid is None else replaced_oid
        if replaced_oid is not None:
            _log_branch_head_replaced(full_ref, replaced_oid, expected)
        self._remote_git(
            (
                "push",
                "--porcelain",
                "--no-verify",
                f"--force-with-lease={full_ref}:{leased}",
                "--",
                self._remote.url,
                f"{expected}:{full_ref}",
            ),
            in_store=True,
        )
        return self._readback_after_send(intent, request, expected)

    def publish(
        self, intent: EffectIntent, request: ReviewedDocumentationPullRequest
    ) -> None:
        """Publish one reviewed replacement tree through the existing push fence."""

        push_intent = self._reviewed_documentation_push_intent(intent, request)
        observed = self.readback(push_intent, ReadbackPhase.BEFORE_SEND)
        if isinstance(observed, EffectReceipt):
            return
        performed = self.execute(push_intent)
        if isinstance(performed, EffectUnknownOutcome):
            raise GitTransportRefused(
                "the reviewed documentation push outcome is unknown"
            )

    def close(self) -> None:
        self._closed = True

    def _replaceable_branch(
        self, intent: EffectIntent, request: PushAtelierCommit
    ) -> EffectAbsence | EffectUnknownOutcome:
        """Whether a branch standing at another commit may carry this push.

        The commit under an Atelier item branch is an earlier attempt's own,
        and what decides whether this attempt may replace it is not git: it is
        whether anyone still reviews it. A branch whose pull requests are all
        closed carries work nobody stands on, and this request holds nothing
        there yet; an open pull request is a person's, and stays the operator's
        reconciliation rather than this adapter's overwrite.
        """

        started = time.monotonic()
        standing = self._head_branch_pull_requests.open_pull_requests_on(
            request.head_branch
        )
        match standing:
            case NoPullRequestOpenOnHeadBranch():
                return EffectAbsence(intent.reference)
            case PullRequestOpenOnHeadBranch(number):
                return EffectUnknownOutcome(
                    intent.reference,
                    UnknownOutcomeReason(
                        None,
                        _elapsed_milliseconds(started),
                        f"pull request #{number} on this branch is open",
                    ),
                )
            case HeadBranchPullRequestsUnreadable(reason):
                return EffectUnknownOutcome(intent.reference, reason)
            case _ as unreachable:
                assert_never(unreachable)

    def _authorized_request(self, intent: EffectIntent) -> PushAtelierCommit:
        if self._closed:
            raise RuntimeError("git transport effect adapter is closed")
        if intent.binding.adapter_binding != self._binding:
            raise EffectIntentMismatch(
                "effect intent does not belong to this adapter binding"
            )
        try:
            return PushAtelierCommit.from_canonical_bytes(intent.request.payload)
        except (TypeError, ValueError) as error:
            raise GitTransportRefused("push request is not canonical") from error

    def _verify_candidate(self, request: PushAtelierCommit) -> None:
        if not self._candidate_store.is_dir():
            raise GitTransportRefused(
                f"candidate store does not exist: {self._candidate_store}"
            )
        reference = f"refs/atelier/candidates/{request.attempt_id}"
        result = self._store_git(("for-each-ref", self._OBJECT_NAME_FORMAT, reference))
        if result.returncode != 0:
            raise GitTransportRefused("the candidate store could not be read")
        standing = result.stdout.decode("ascii", errors="strict").strip()
        if standing != request.candidate_tree:
            raise GitTransportRefused(
                "the pinned attempt does not name the declared candidate tree"
            )

    def _reviewed_documentation_push_intent(
        self,
        intent: EffectIntent,
        request: ReviewedDocumentationPullRequest,
    ) -> EffectIntent:
        self._ensure_candidate_store(request.base_revision)
        self._fetch_base(request.base_revision)
        candidate_tree = self._reviewed_candidate_tree(request)
        self._anchor_reviewed_candidate(request.candidate_digest, candidate_tree)
        author, committer, completed_at = self._base_commit_identity(
            request.base_revision
        )
        push = PushAtelierCommit(
            request.candidate_digest,
            candidate_tree,
            request.base_revision,
            request.head_branch,
            author,
            committer,
            completed_at,
        )
        return EffectIntent(
            EffectBinding(
                LogicalEffectKey(
                    f"{intent.binding.logical_key.value}/documentation-push"
                ),
                intent.binding.run_id,
                intent.binding.workflow_revision_hash,
                self._binding.adapter_revision,
                self._binding.destination,
                self._binding.operational_identity,
                self._binding.operation_name,
            ),
            CanonicalRequest(push.canonical_bytes()),
        )

    def _ensure_candidate_store(self, base_revision: str) -> None:
        if self._candidate_store.is_dir():
            return
        if self._candidate_store.exists():
            raise GitTransportRefused("the candidate store path is not a directory")
        self._candidate_store.parent.mkdir(parents=True, exist_ok=True)
        object_format = "sha1" if len(base_revision) == 40 else "sha256"
        result = self._command_runner.run(
            (
                *_HOOK_FREE_ARGUMENTS,
                "init",
                "--bare",
                "--quiet",
                f"--object-format={object_format}",
                "--",
                str(self._candidate_store),
            ),
            working_directory=self._candidate_store.parent,
            environment=isolated_git_environment(),
        )
        if result.returncode != 0:
            raise GitTransportRefused("the candidate store could not be initialized")

    def _reviewed_candidate_tree(
        self, request: ReviewedDocumentationPullRequest
    ) -> str:
        with tempfile.TemporaryDirectory(
            dir=self._candidate_store.parent,
            prefix=".atelier2-reviewed-documentation-",
        ) as temporary:
            index = Path(temporary) / "index"
            self._index_git(index, ("read-tree", request.base_revision))
            for replacement in request.replacements:
                staged = self._index_git(
                    index, ("ls-files", "--stage", "-z", "--", replacement.path)
                )
                mode, object_id = self._staged_blob(staged, replacement.path)
                current = self._store_git(("cat-file", "blob", object_id))
                if current.returncode != 0:
                    raise GitTransportRefused(
                        f"the current blob for {replacement.path} could not be read"
                    )
                if (
                    hashlib.sha256(current.stdout).hexdigest()
                    != replacement.current_digest
                ):
                    raise GitTransportRefused(
                        f"the current digest for {replacement.path} changed"
                    )
                written = self._store_git(
                    ("hash-object", "-w", "--stdin"), replacement.replacement
                )
                if written.returncode != 0:
                    raise GitTransportRefused(
                        f"the replacement blob for {replacement.path} could not be stored"
                    )
                replacement_object = written.stdout.decode(
                    "ascii", errors="strict"
                ).strip()
                self._index_git(
                    index,
                    (
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        mode,
                        replacement_object,
                        replacement.path,
                    ),
                )
            return (
                self._index_git(index, ("write-tree",))
                .decode("ascii", errors="strict")
                .strip()
            )

    def _index_git(self, index: Path, arguments: tuple[str, ...]) -> bytes:
        result = self._command_runner.run(
            (*_HOOK_FREE_ARGUMENTS, *arguments),
            working_directory=self._candidate_store.parent,
            environment=isolated_git_environment(
                GIT_DIR=str(self._candidate_store), GIT_INDEX_FILE=str(index)
            ),
        )
        if result.returncode != 0:
            raise GitTransportRefused(
                f"the reviewed documentation index refused {arguments[0]}"
            )
        return result.stdout

    @staticmethod
    def _staged_blob(staged: bytes, path: str) -> tuple[str, str]:
        records = staged.rstrip(b"\0").split(b"\0") if staged else []
        if len(records) != 1:
            raise GitTransportRefused(
                f"the reviewed document {path} is not one existing regular file"
            )
        metadata, separator, recorded_path = records[0].partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3 or recorded_path.decode() != path:
            raise GitTransportRefused(
                f"the reviewed document {path} has an unreadable index entry"
            )
        mode, object_id, stage = (field.decode("ascii") for field in fields)
        if stage != "0" or mode not in {"100644", "100755"}:
            raise GitTransportRefused(
                f"the reviewed document {path} is not one existing regular file"
            )
        return mode, object_id

    def _anchor_reviewed_candidate(self, attempt_id: str, tree: str) -> None:
        reference = f"refs/atelier/candidates/{attempt_id}"
        standing = self._store_git(
            ("for-each-ref", self._OBJECT_NAME_FORMAT, reference)
        )
        if standing.returncode != 0:
            raise GitTransportRefused("the reviewed candidate ref could not be read")
        current = standing.stdout.decode("ascii", errors="strict").strip()
        if current:
            if current != tree:
                raise GitTransportRefused(
                    "the reviewed candidate digest already names another tree"
                )
            return
        written = self._store_git(("update-ref", reference, tree, ""))
        if written.returncode != 0:
            standing = self._store_git(
                ("for-each-ref", self._OBJECT_NAME_FORMAT, reference)
            )
            if (
                standing.returncode != 0
                or standing.stdout.decode("ascii", errors="strict").strip() != tree
            ):
                raise GitTransportRefused(
                    "the reviewed candidate ref could not be anchored"
                )

    def _base_commit_identity(
        self, base_revision: str
    ) -> tuple[GitCommitIdentity, GitCommitIdentity, str]:
        result = self._store_git(
            (
                "show",
                "-s",
                "--format=%an%x00%ae%x00%cn%x00%ce%x00%ct",
                base_revision,
            )
        )
        if result.returncode != 0:
            raise GitTransportRefused("the base commit identity could not be read")
        fields = result.stdout.rstrip(b"\n").split(b"\0")
        if len(fields) != 5:
            raise GitTransportRefused("the base commit identity is malformed")
        try:
            author = GitCommitIdentity(fields[0].decode(), fields[1].decode())
            committer = GitCommitIdentity(fields[2].decode(), fields[3].decode())
            completed_at = datetime.fromtimestamp(int(fields[4]), UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise GitTransportRefused(
                "the base commit identity is malformed"
            ) from error
        return author, committer, completed_at

    def _remote_ref(self, full_ref: str) -> _RemoteRefObservation:
        started = time.monotonic()
        result = self._remote_git(
            ("ls-remote", "--refs", "--", self._remote.url, full_ref)
        )
        elapsed = _elapsed_milliseconds(started)
        if result.returncode != 0:
            return _unreadable(result, elapsed)
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        if not lines:
            return _RemoteRefAbsent(result.returncode, elapsed)
        if len(lines) != 1:
            return _unreadable(result, elapsed)
        oid, separator, name = lines[0].partition("\t")
        if (
            separator != "\t"
            or name != full_ref
            or _GIT_OBJECT_ID.fullmatch(oid) is None
        ):
            return _unreadable(result, elapsed)
        return _RemoteRefFound(oid)

    def _fetch_base(self, oid: str) -> None:
        result = self._remote_git(
            (
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--",
                self._remote.url,
                oid,
            ),
            in_store=True,
        )
        if result.returncode != 0:
            raise GitTransportRefused("the declared base commit could not be fetched")

    def _store_commit(self, request: PushAtelierCommit, request_hash: str) -> str:
        result = self._store_git(
            ("hash-object", "-t", "commit", "-w", "--stdin"),
            request.commit_bytes(request_hash),
        )
        if result.returncode != 0:
            raise GitTransportRefused("the deterministic commit could not be stored")
        return result.stdout.decode("ascii", errors="strict").strip()

    def _performed(
        self, request: PushAtelierCommit, commit_oid: str
    ) -> PerformedEffect:
        receipt = PushAtelierCommitReceipt(
            self._remote.identity,
            request.head_branch.full_ref,
            commit_oid,
            request.base_commit,
            request.candidate_tree,
            request.head_branch.value,
            request.author,
            request.committer,
        )
        return PerformedEffect(
            EffectId(commit_oid), EffectResult(receipt.result_bytes())
        )

    def _readback_after_send(
        self, intent: EffectIntent, request: PushAtelierCommit, expected: str
    ) -> PerformedEffect | EffectUnknownOutcome:
        observation = self._remote_ref(request.head_branch.full_ref)
        if isinstance(observation, _RemoteRefFound) and observation.oid == expected:
            return self._performed(request, expected)
        return _unknown(intent, observation)

    def _credential_arguments(self) -> tuple[str, ...]:
        credential_file = self._remote.credential_file
        if credential_file is None:
            return ()
        path = shlex.quote(str(credential_file))
        helper = (
            '!f() { test "$1" = get || exit 0; '
            "printf 'username=x-access-token\\npassword='; "
            f"/bin/cat {path}; printf '\\n'; }}; f"
        )
        arguments = ["-c", f"credential.helper={helper}"]
        return tuple(arguments)

    def _remote_git(
        self, arguments: tuple[str, ...], *, in_store: bool = False
    ) -> GitCommandResult:
        _require_a_readable_token(self._remote.credential_file)
        prefix = (*_HOOK_FREE_ARGUMENTS, *self._credential_arguments())
        environment = isolated_git_environment()
        if in_store:
            environment["GIT_DIR"] = str(self._candidate_store)
        return self._command_runner.run(
            (*prefix, *arguments),
            working_directory=self._candidate_store.parent,
            environment=environment,
        )

    def _store_git(
        self, arguments: tuple[str, ...], standard_input: bytes | None = None
    ) -> GitCommandResult:
        return self._command_runner.run(
            (*_HOOK_FREE_ARGUMENTS, *arguments),
            working_directory=self._candidate_store.parent,
            environment=isolated_git_environment(GIT_DIR=str(self._candidate_store)),
            standard_input=standard_input,
        )
