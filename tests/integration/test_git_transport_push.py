"""The create-only git transport publishes one deterministic candidate commit."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import pytest

from atelier2.adapters.git_transport.effects import (
    GitCommandResult,
    GitCommandRunner,
    GitCredentialUnresolvable,
    GitRemote,
    GitTransportEffectAdapterFactory,
    SubprocessGitCommandRunner,
)
from atelier2.contracts.effect_markers import marker_line
from atelier2.contracts.effect_requests import (
    GitCommitIdentity,
    HeadBranch,
    PushAtelierCommit,
    ReviewedDocumentationPullRequest,
    ReviewedDocumentReplacement,
    reviewed_documentation_candidate_digest,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectAbsence,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectReceipt,
    EffectUnknownOutcome,
    LogicalEffectKey,
    PerformedEffect,
    ReadbackPhase,
    UnknownOutcomeReason,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.contracts.secret_redaction import REDACTION_MARKER
from atelier2.ports.effects import (
    HeadBranchPullRequestState,
    HeadBranchPullRequestsUnreadable,
    PullRequestOpenOnHeadBranch,
)
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests

ATTEMPT_ID = "a1" * 32
HEAD_BRANCH = HeadBranch("atelier2/work-item/" + "b2" * 32)
AUTHOR = GitCommitIdentity("Atelier Agent", "agent@example.test")
COMMITTER = GitCommitIdentity("Atelier Core", "core@example.test")


def _git(repository: Path, *arguments: str, stdin: bytes | None = None) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
    }
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env=environment,
        input=stdin,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode().strip()


def _repositories(root: Path) -> tuple[Path, Path, str, str]:
    source = root / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=main")
    (source / "kept.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "kept.txt")
    _git(source, "commit", "--quiet", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    remote = root / "remote.git"
    _git(root, "init", "--bare", "--quiet", str(remote))
    _git(source, "push", "--quiet", str(remote), "HEAD:refs/heads/main")

    (source / "kept.txt").write_text("candidate exact bytes\n", encoding="utf-8")
    (source / "new.txt").write_bytes(b"\x00candidate\n")
    _git(source, "add", "--all")
    candidate_tree = _git(source, "write-tree")
    candidate_commit = _git(
        source, "commit-tree", candidate_tree, "-p", base, stdin=b"candidate\n"
    )
    store = root / "candidates.git"
    _git(root, "init", "--bare", "--quiet", str(store))
    _git(store, "fetch", "--quiet", str(source), candidate_commit)
    _git(store, "update-ref", f"refs/atelier/candidates/{ATTEMPT_ID}", candidate_tree)
    return store, remote, base, candidate_tree


def _intent(
    factory: GitTransportEffectAdapterFactory, base: str, tree: str
) -> tuple[EffectIntent, PushAtelierCommit]:
    request = PushAtelierCommit(
        ATTEMPT_ID,
        tree,
        base,
        HEAD_BRANCH,
        AUTHOR,
        COMMITTER,
        "2026-08-27T12:34:56Z",
    )
    return (
        EffectIntent(
            EffectBinding(
                LogicalEffectKey("run/push"),
                RunId("run"),
                WorkflowRevision(b"workflow").revision_hash,
                factory.binding.adapter_revision,
                factory.binding.destination,
                factory.binding.operational_identity,
                factory.binding.operation_name,
            ),
            CanonicalRequest(request.canonical_bytes()),
        ),
        request,
    )


def _factory(
    store: Path,
    remote: Path,
    runner: GitCommandRunner | None = None,
    pull_requests: FakeHeadBranchPullRequests | None = None,
    credential_file: Path | None = None,
) -> GitTransportEffectAdapterFactory:
    arguments = (
        store,
        GitRemote("local-test", str(remote), credential_file),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        pull_requests or FakeHeadBranchPullRequests(),
    )
    return (
        GitTransportEffectAdapterFactory(*arguments)
        if runner is None
        else GitTransportEffectAdapterFactory(*arguments, runner)
    )


def _foreign_commit(
    root: Path, base: str, tree: str, remote: Path, message: bytes
) -> str:
    """An earlier attempt's own commit, standing on the item's branch."""

    commit = _git(root / "source", "commit-tree", tree, "-p", base, stdin=message)
    _git(
        root / "source",
        "push",
        "--quiet",
        str(remote),
        f"{commit}:{HEAD_BRANCH.full_ref}",
    )
    return commit


def test_push_creates_the_declared_commit_and_replay_finds_no_twin(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    factory = _factory(store, remote)
    intent, request = _intent(factory, base, tree)
    expected = request.expected_commit_oid(intent.request.request_hash.value)
    adapter = factory.open()
    try:
        assert isinstance(
            adapter.readback(intent, ReadbackPhase.BEFORE_SEND), EffectAbsence
        )
        performed = adapter.execute(intent)
        replay = adapter.execute(intent)
        readback = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        adapter.close()

    assert isinstance(performed, PerformedEffect)
    assert replay == performed
    assert performed.effect_id.value == expected
    assert isinstance(readback, EffectReceipt)
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == expected
    assert _git(remote, "rev-parse", f"{expected}^{{tree}}") == tree
    assert _git(remote, "rev-parse", f"{expected}^") == base
    commit = _git(remote, "cat-file", "commit", expected)
    assert f"author {AUTHOR.name} <{AUTHOR.email}>" in commit
    assert f"committer {COMMITTER.name} <{COMMITTER.email}>" in commit
    assert marker_line(intent.request.request_hash.value) in commit.splitlines()


def test_a_branch_no_open_pull_request_reviews_is_replaced_under_a_lease_on_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    foreign = _foreign_commit(tmp_path, base, tree, remote, b"an earlier attempt\n")
    pull_requests = FakeHeadBranchPullRequests()
    runner = _ScriptedRemoteReadRunner([])
    factory = _factory(store, remote, runner, pull_requests)
    intent, request = _intent(factory, base, tree)
    expected = request.expected_commit_oid(intent.request.request_hash.value)
    adapter = factory.open()
    try:
        observed = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
        with caplog.at_level(logging.INFO, logger="atelier2"):
            performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(observed, EffectAbsence)
    assert isinstance(performed, PerformedEffect)
    assert performed.effect_id.value == expected
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == expected
    assert pull_requests.asked == [HEAD_BRANCH, HEAD_BRANCH]
    # ADR 0010's 2026-09-05 amendment: the replaced oid is evidence for an
    # operator, not part of the receipt's identity, so it is logged at the
    # send rather than carried in the result bytes a crash readback must
    # reconstruct identically without ever observing it.
    replaced_head_logs = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "git_transport_push_replaced_branch_head"
    ]
    assert len(replaced_head_logs) == 1
    assert replaced_head_logs[0].__dict__["replaced_oid"] == foreign
    assert len(runner.push_arguments) == 1
    assert (
        f"--force-with-lease={HEAD_BRANCH.full_ref}:{foreign}"
        in runner.push_arguments[0]
    )
    assert factory.binding.operational_identity == AdapterOperationalIdentity(
        "local-test"
    )


@pytest.mark.parametrize(
    ("standing", "detail"),
    [
        pytest.param(
            PullRequestOpenOnHeadBranch(4711),
            "pull request #4711 on this branch is open",
            id="a-reviewer-still-stands-on-it",
        ),
        pytest.param(
            HeadBranchPullRequestsUnreadable(
                UnknownOutcomeReason(503, 12, "the tracker answered nothing readable")
            ),
            "the tracker answered nothing readable",
            id="the-tracker-could-not-answer",
        ),
    ],
)
def test_a_branch_this_push_may_not_replace_sends_nothing_and_keeps_the_reason(
    tmp_path: Path, standing: HeadBranchPullRequestState, detail: str
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    foreign = _foreign_commit(tmp_path, base, tree, remote, b"an earlier attempt\n")
    runner = _ScriptedRemoteReadRunner([])
    factory = _factory(store, remote, runner, FakeHeadBranchPullRequests(standing))
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        observed = adapter.readback(intent, ReadbackPhase.BEFORE_SEND)
        result = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(observed, EffectUnknownOutcome)
    assert isinstance(result, EffectUnknownOutcome)
    for outcome in (observed, result):
        assert outcome.reason is not None
        assert outcome.reason.detail == detail
    assert runner.push_arguments == []
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == foreign


def test_a_branch_that_moves_after_the_read_fails_the_lease_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    foreign = _foreign_commit(tmp_path, base, tree, remote, b"an earlier attempt\n")
    racing = _git(
        tmp_path / "source", "commit-tree", tree, "-p", base, stdin=b"a third writer\n"
    )
    _git(
        tmp_path / "source",
        "push",
        "--quiet",
        str(remote),
        f"{racing}:refs/heads/racing",
    )
    runner = _MovesTheBranchBeforeEachPush(remote, racing)
    factory = _factory(store, remote, runner)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        result = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(result, EffectUnknownOutcome)
    assert runner.pushes == 1
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == racing
    assert racing != foreign


def test_a_foreign_oid_read_after_send_stays_unknown_without_asking_the_tracker(
    tmp_path: Path,
) -> None:
    """A branch someone else moved and one this send lost a race for read alike.

    The pre-send lease amendment only ever licenses a *pre-send* replacement:
    a foreign commit read after a send already happened is still the
    divergence the create-only fence never resolves for itself, so the
    tracker is never asked and the branch is left exactly as read.
    """

    store, remote, base, tree = _repositories(tmp_path)
    foreign = _foreign_commit(tmp_path, base, tree, remote, b"a foreign write\n")
    pull_requests = FakeHeadBranchPullRequests()
    factory = _factory(store, remote, pull_requests=pull_requests)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        observed = adapter.readback(intent, ReadbackPhase.AFTER_SEND)
    finally:
        adapter.close()

    assert isinstance(observed, EffectUnknownOutcome)
    assert pull_requests.asked == []
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == foreign


def test_concurrent_replay_publishes_one_commit_and_no_twin(tmp_path: Path) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    factory = _factory(store, remote)
    intent, request = _intent(factory, base, tree)

    def publish() -> object:
        adapter = factory.open()
        try:
            return adapter.execute(intent)
        finally:
            adapter.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = tuple(workers.map(lambda _index: publish(), range(2)))

    expected = request.expected_commit_oid(intent.request.request_hash.value)
    assert all(
        isinstance(result, PerformedEffect) and result.effect_id.value == expected
        for result in results
    )
    assert (
        _git(remote, "for-each-ref", "--format=%(objectname)", HEAD_BRANCH.full_ref)
        == expected
    )


@dataclass
class _ScriptedRemoteReadRunner:
    remote_reads: list[GitCommandResult | None]
    delegate: SubprocessGitCommandRunner = field(
        default_factory=SubprocessGitCommandRunner
    )
    push_arguments: list[tuple[str, ...]] = field(default_factory=list)
    remote_arguments: list[tuple[str, ...]] = field(default_factory=list)

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        if "ls-remote" in arguments:
            self.remote_arguments.append(arguments)
            scripted = self.remote_reads.pop(0) if self.remote_reads else None
            if scripted is not None:
                return scripted
        elif "fetch" in arguments:
            self.remote_arguments.append(arguments)
        if "push" in arguments:
            self.push_arguments.append(arguments)
        return self.delegate.run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )


class _MovesTheBranchBeforeEachPush(SubprocessGitCommandRunner):
    """A second writer that takes the branch between this adapter's read and its send."""

    def __init__(self, remote: Path, racing_commit: str) -> None:
        self._remote = remote
        self._racing_commit = racing_commit
        self.pushes = 0

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        if "push" in arguments:
            self.pushes += 1
            _git(self._remote, "update-ref", HEAD_BRANCH.full_ref, self._racing_commit)
        return super().run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )


def test_inconclusive_read_after_send_reconciles_and_retry_sends_no_second_push(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    inconclusive = GitCommandResult(1, b"", b"remote unavailable")
    runner = _ScriptedRemoteReadRunner([None, inconclusive, inconclusive])
    factory = _factory(store, remote, runner)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        first = adapter.execute(intent)
        retry = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(first, EffectUnknownOutcome)
    assert isinstance(retry, EffectUnknownOutcome)
    assert len(runner.push_arguments) == 1
    for outcome in (first, retry):
        assert outcome.reason is not None
        assert outcome.reason.failure_code == inconclusive.returncode
        assert outcome.reason.detail == inconclusive.stderr.decode()


def _reviewed_documentation_request(base: str) -> ReviewedDocumentationPullRequest:
    replacement = ReviewedDocumentReplacement(
        "kept.txt", sha256(b"base\n").hexdigest(), b"reviewed exact bytes\n"
    )
    title = "Reviewed documentation"
    body = "The approved replacement."
    return ReviewedDocumentationPullRequest(
        base,
        reviewed_documentation_candidate_digest(base, (replacement,), title, body),
        "d" * 64,
        (replacement,),
        title,
        body,
        HEAD_BRANCH,
    )


def test_a_reviewed_documentation_request_reuses_the_push_fence_for_exact_bytes(
    tmp_path: Path,
) -> None:
    store, remote, base, _tree = _repositories(tmp_path)
    runner = _ScriptedRemoteReadRunner([None] * 8)
    factory = _factory(store, remote, runner)
    request = _reviewed_documentation_request(base)
    outer_intent, _push_request = _intent(factory, base, _tree)
    adapter = factory.open()
    try:
        adapter.publish(outer_intent, request)
        adapter.publish(outer_intent, request)
    finally:
        adapter.close()

    commit = _git(remote, "rev-parse", HEAD_BRANCH.full_ref)
    assert _git(remote, "rev-parse", f"{commit}^") == base
    assert _git(remote, "show", f"{commit}:kept.txt") == "reviewed exact bytes"
    assert len(runner.push_arguments) == 1


def test_a_remote_answering_that_it_holds_no_such_ref_licenses_one_push(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    runner = _ScriptedRemoteReadRunner([GitCommandResult(0, b"", b""), None])
    factory = _factory(store, remote, runner)
    intent, request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(performed, PerformedEffect)
    assert performed.effect_id.value == request.expected_commit_oid(
        intent.request.request_hash.value
    )
    assert len(runner.push_arguments) == 1


@pytest.mark.parametrize(
    ("observation", "detail"),
    [
        pytest.param(
            GitCommandResult(128, b"", b"fatal: could not read from remote"),
            "fatal: could not read from remote",
            id="refused-read",
        ),
        pytest.param(
            GitCommandResult(
                0,
                (
                    b"a" * 40
                    + b"\t"
                    + HEAD_BRANCH.full_ref.encode()
                    + b"\n"
                    + b"b" * 40
                    + b"\trefs/heads/other\n"
                ),
                b"",
            ),
            (
                "a" * 40
                + "\t"
                + HEAD_BRANCH.full_ref
                + "\n"
                + "b" * 40
                + "\trefs/heads/other"
            ),
            id="multiple-lines",
        ),
        pytest.param(
            GitCommandResult(0, b"a" * 40 + b"\trefs/heads/other\n", b""),
            "a" * 40 + "\trefs/heads/other",
            id="wrong-ref",
        ),
        pytest.param(
            GitCommandResult(
                0, b"not-an-oid\t" + HEAD_BRANCH.full_ref.encode() + b"\n", b""
            ),
            "not-an-oid\t" + HEAD_BRANCH.full_ref,
            id="malformed-oid",
        ),
    ],
)
def test_a_remote_read_that_resolves_nothing_sends_nothing_and_keeps_what_git_said(
    tmp_path: Path, observation: GitCommandResult, detail: str
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    runner = _ScriptedRemoteReadRunner([observation])
    factory = _factory(store, remote, runner)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        result = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(result, EffectUnknownOutcome)
    assert runner.push_arguments == []
    assert result.reason is not None
    assert result.reason.failure_code == observation.returncode
    assert result.reason.detail == detail
    assert result.reason.duration_milliseconds >= 0


def test_a_credential_git_printed_never_reaches_the_kept_reason(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    token = "ghp_" + "s" * 36
    refusal = f"fatal: authentication failed for 'https://x-access-token:{token}@host'"
    runner = _ScriptedRemoteReadRunner([GitCommandResult(128, b"", refusal.encode())])
    factory = _factory(store, remote, runner)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        result = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(result, EffectUnknownOutcome)
    assert runner.push_arguments == []
    assert result.reason is not None
    assert token not in result.reason.detail
    assert REDACTION_MARKER in result.reason.detail


@pytest.mark.parametrize(
    ("operation", "token_file"),
    [
        pytest.param("readback", None, id="missing-readback"),
        pytest.param("execute", None, id="missing-execute"),
        pytest.param("reviewed-publish", None, id="missing-reviewed-publish"),
        pytest.param("execute", b"", id="empty"),
        pytest.param("execute", b" \n", id="whitespace"),
        pytest.param("execute", b"\xc2\xa0\n", id="unicode-space-only"),
        pytest.param("execute", b"ghp_token\xff", id="invalid-utf8"),
        pytest.param("execute", b"ghp_one\nghp_two", id="embedded-newline"),
    ],
)
def test_without_one_well_formed_token_no_git_process_reaches_the_remote(
    tmp_path: Path, operation: str, token_file: bytes | None
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    credential_file = tmp_path / "token"
    if token_file is not None:
        credential_file.write_bytes(token_file)
    runner = _ScriptedRemoteReadRunner([])
    factory = _factory(store, remote, runner, credential_file=credential_file)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    reaching_the_remote = {
        "readback": lambda: adapter.readback(intent, ReadbackPhase.BEFORE_SEND),
        "execute": lambda: adapter.execute(intent),
        "reviewed-publish": lambda: adapter.publish(
            intent, _reviewed_documentation_request(base)
        ),
    }
    try:
        with pytest.raises(GitCredentialUnresolvable):
            reaching_the_remote[operation]()
    finally:
        adapter.close()

    assert runner.remote_arguments == []
    assert runner.push_arguments == []


def test_a_token_set_after_the_adapter_opened_licenses_the_next_push(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    credential_file = tmp_path / "token"
    factory = _factory(store, remote, credential_file=credential_file)
    intent, request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        credential_file.write_text("gho_scenario_token", encoding="utf-8")
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(performed, PerformedEffect)
    assert _git(remote, "rev-parse", HEAD_BRANCH.full_ref) == (
        request.expected_commit_oid(intent.request.request_hash.value)
    )


def test_a_reachable_base_that_is_no_longer_an_advertised_tip_can_be_pushed(
    tmp_path: Path,
) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    source = tmp_path / "source"
    (source / "later.txt").write_text("default branch moved\n", encoding="utf-8")
    _git(source, "add", "later.txt")
    _git(source, "commit", "--quiet", "-m", "move the default branch")
    _git(source, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    assert _git(remote, "rev-parse", "refs/heads/main") != base

    factory = _factory(store, remote)
    intent, request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(performed, PerformedEffect)
    assert performed.effect_id.value == request.expected_commit_oid(
        intent.request.request_hash.value
    )


def test_remote_git_positionals_follow_the_option_terminator(tmp_path: Path) -> None:
    store, remote, base, tree = _repositories(tmp_path)
    runner = _ScriptedRemoteReadRunner([None, None])
    factory = _factory(store, remote, runner)
    intent, _request = _intent(factory, base, tree)
    adapter = factory.open()
    try:
        performed = adapter.execute(intent)
    finally:
        adapter.close()

    assert isinstance(performed, PerformedEffect)
    remote_commands = [*runner.remote_arguments, *runner.push_arguments]
    assert remote_commands
    for arguments in remote_commands:
        separator = arguments.index("--")
        remote_position = arguments.index(str(remote))
        assert separator < remote_position


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("url", "--upload-pack=malicious", id="url-leading-dash"),
        pytest.param("ref", "malicious:refs/heads/main", id="ref-colon"),
        pytest.param("ref", "-malicious", id="ref-leading-dash"),
        pytest.param("oid", "-" + "a" * 39, id="oid-leading-dash"),
        pytest.param("oid", "a" * 20 + ":" + "a" * 19, id="oid-colon"),
    ],
)
def test_git_positional_injection_shapes_are_refused(field: str, value: str) -> None:
    if field == "url":
        with pytest.raises(ValueError, match="URL"):
            GitRemote("remote", value)
        return
    if field == "ref":
        with pytest.raises(ValueError, match="unsafe branch"):
            HeadBranch(value)
        return
    with pytest.raises(ValueError, match="git object format"):
        PushAtelierCommit(
            ATTEMPT_ID,
            "a" * 40,
            value,
            HEAD_BRANCH,
            AUTHOR,
            COMMITTER,
            "2026-08-27T12:34:56Z",
        )
