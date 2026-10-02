"""The project's own manifest decides what verifies it, and says so or refuses.

Three questions live here. What does this project declare -- answered by reading
the manifest the pinned commit carries, and refused in that manifest's own words
where it declares nothing. Which manifest is that -- answered by the pin alone, so
an edit sitting in the operator's checkout decides nothing about a started run.
And when is it asked -- answered by the attempt, which attests both the pin and
the verification beside the scratch root: before any provider process, and before
the claim that makes an attempt durable, so a project that declares nothing and a
pin that no longer resolves each cost no run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from atelier2.adapters.candidate_store import GitCandidateTreeStore
from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.adapters.project_verification import (
    MAXIMUM_VERIFICATION_OUTPUT_BYTES,
    PROJECT_MANIFEST_NAME,
    LocalProjectVerificationRunner,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    TranscriptMomentOrigin,
    TranscriptRecordedMoment,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
    AgentExecutionResult,
)
from atelier2.contracts.artifacts import Artifact
from atelier2.contracts.candidate_reports import CANDIDATE_DIFF_TRUNCATION_MARKER
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.secret_redaction import REDACTION_MARKER
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
    ToolRedemptionReceipt,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptClaimResult,
    AgentAttemptFailed,
    AgentAttemptSucceeded,
    ProjectVerificationFailureEvidence,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    PermissionDecider,
    PrintModeExecutor,
)
from atelier2.ports.artifacts import ArtifactCreated, PublishArtifactResult
from atelier2.ports.candidate_store import (
    CANDIDATE_DIFF_READ_BYTES,
    MAXIMUM_CANDIDATE_DIFF_BYTES,
    CandidateTreeStore,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.project_source import ProjectSourceUnavailable
from atelier2.ports.project_verification import (
    MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES,
    PinnedProjectSource,
    ProjectVerificationOutcome,
    ProjectVerificationUnavailable,
    ProjectVerificationUndeclared,
    pytest_summary_line,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    leased_directory_identity,
    prepared_agent_attempt,
    workspace_files_nobody_opens,
)
from tests.scenarios.credentials import assembled
from tests.scenarios.projects import (
    CandidatesKeptInMemory,
    declaring_verification,
    git_project,
    write_into_checkout,
)

THE_GRANT = DeclaredToolGrant(
    PublishedRevisionHash("c3" * 32), ToolGrantCapability.RUN_PROJECT_VERIFICATION
)
A_PIN_NO_SOURCE_ANSWERS_FOR = ProjectSourcePin("f0" * 20, "e1" * 20)


PYTEST_SUMMARY_LINE_CASES: tuple[tuple[str, bytes, str | None], ...] = (
    (
        "a real pytest -q run's own last line, no warnings",
        (
            b"F.\n"
            b"=========================== short test summary info ============================\n"
            b"FAILED tests/test_sample.py::test_fail\n"
            b"1 failed, 1 passed in 0.06s\n"
        ),
        "1 failed, 1 passed in 0.06s",
    ),
    (
        "a real pytest -q run whose last line follows a warnings section",
        (
            b"F.w\n"
            b"=============================== warnings summary ===============================\n"
            b"tests/test_sample.py::test_warns\n"
            b"  tests/test_sample.py:10: DeprecationWarning: deprecated thing\n"
            b"\n"
            b"=========================== short test summary info ============================\n"
            b"FAILED tests/test_sample.py::test_fail\n"
            b"1 failed, 2 passed, 1 warning in 0.09s\n"
        ),
        "1 failed, 2 passed, 1 warning in 0.09s",
    ),
    (
        "a real pytest -n auto run: the same bare verdict, no xdist decoration",
        (
            b"..F                                                        [100%]\n"
            b"1 failed, 2 passed, 1 warning in 7.51s\n"
        ),
        "1 failed, 2 passed, 1 warning in 7.51s",
    ),
    (
        "a real pytest -q run that collected nothing at all",
        b"\nno tests ran in 0.00s\n",
        "no tests ran in 0.00s",
    ),
    (
        "a real run long enough to also carry the H:MM:SS parenthetical",
        b"1 passed in 61.01s (0:01:01)\n",
        "1 passed in 61.01s (0:01:01)",
    ),
    (
        "a bracketed section header that names no verdict count of its own",
        b"=============================== warnings summary ===============================\n",
        None,
    ),
    ("plain output naming no test at all", b"hello from a build script\n", None),
    ("empty output", b"", None),
)


@pytest.mark.parametrize(
    ("label", "tail", "expected"),
    PYTEST_SUMMARY_LINE_CASES,
    ids=[label for label, _, _ in PYTEST_SUMMARY_LINE_CASES],
)
def test_pytest_summary_line_reads_the_runs_own_last_verdict(
    label: str, tail: bytes, expected: str | None
) -> None:
    del label
    assert pytest_summary_line(tail) == expected


def test_bracketed_pytest_headers_still_yield_the_same_verdict() -> None:
    """Real pytest tails from this module's cases still yield the same verdict."""

    for label, tail, expected in PYTEST_SUMMARY_LINE_CASES:
        assert pytest_summary_line(tail) == expected, label


def test_a_long_unmatched_line_does_not_cost_superlinear_time() -> None:
    """A long line that starts with '=' and never closes must stay cheap.

    Verification output is untrusted and can run to thousands of characters; a
    cubic scan of twenty thousand spaces would miss this bound by minutes.
    """

    line = ("=" + " " * 20_000).encode()
    started = time.perf_counter()
    assert pytest_summary_line(line) is None
    elapsed = time.perf_counter() - started
    assert elapsed < 0.1


def runner_for(root: Path) -> LocalProjectVerificationRunner:
    return LocalProjectVerificationRunner(LocalGitProjectSource(root))


def _outcome_facts(
    outcome: ProjectVerificationOutcome,
) -> tuple[tuple[str, ...], int, Sha256Hash]:
    """The command, exit code and full-output digest this suite has always pinned.

    `duration_seconds` is real elapsed time and cannot be a literal in a test, so
    it is asserted separately (merely non-negative) rather than folded in here.
    """

    return outcome.command, outcome.exit_code, outcome.standard_output_hash


STATES_NO_VERIFICATION: tuple[tuple[str, str], ...] = (
    ("a manifest naming no atelier section", "[tool.pytest]\naddopts = '-q'\n"),
    ("a section naming no verification", "[tool.atelier2]\nname = 'this project'\n"),
    (
        "a verification naming no command",
        "[tool.atelier2.verification]\ntimeout_seconds = 30\n",
    ),
    (
        "a command that is not a command",
        "[tool.atelier2.verification]\ncommand = 'run the tests'\ntimeout_seconds = 30\n",
    ),
    (
        "a command carrying an empty argument",
        '[tool.atelier2.verification]\ncommand = ["/bin/sh", ""]\ntimeout_seconds = 30\n',
    ),
    (
        "a verification naming no deadline",
        '[tool.atelier2.verification]\ncommand = ["/bin/true"]\n',
    ),
    (
        "a deadline that never expires",
        '[tool.atelier2.verification]\ncommand = ["/bin/true"]\ntimeout_seconds = 0\n',
    ),
    ("a manifest that is not a manifest", "[tool.atelier2\n"),
)


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
@pytest.mark.parametrize(
    ("label", "body"),
    STATES_NO_VERIFICATION,
    ids=[label for label, _ in STATES_NO_VERIFICATION],
)
def test_a_project_stating_no_verification_is_refused_in_its_manifests_words(
    tmp_path: Path, label: str, body: str
) -> None:
    del label
    root = tmp_path / "project"
    pin = git_project(root, {PROJECT_MANIFEST_NAME: body})

    with pytest.raises(ProjectVerificationUndeclared, match=PROJECT_MANIFEST_NAME):
        runner_for(root).preflight(pin)


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
def test_a_commit_carrying_no_manifest_at_all_is_refused_by_the_commit_it_named(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, {"README.md": "a project that never declared one\n"})

    with pytest.raises(ProjectVerificationUndeclared, match="no project manifest"):
        runner_for(root).preflight(pin)


@pytest.mark.proves("what-a-project-declares-and-where-it-runs-are-one-commit")
def test_the_declaration_read_is_the_pinned_commits_and_not_the_checkouts(
    tmp_path: Path,
) -> None:
    """An edit nobody committed decides nothing about a run already pinned."""

    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    write_into_checkout(root, {PROJECT_MANIFEST_NAME: "[tool.pytest]\naddopts = ''\n"})

    runner_for(root).preflight(pin)


@pytest.mark.proves("what-a-project-declares-and-where-it-runs-are-one-commit")
def test_a_file_written_on_the_lease_after_materialize_is_visible_to_the_command(
    tmp_path: Path,
) -> None:
    """A file the pin never carried is what the command sees, once it is on the lease."""

    root = tmp_path / "project"
    pin = git_project(
        root, declaring_verification(["/bin/cat", "written-after-materialize.txt"])
    )
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a3" * 32), lease_directory)
    LocalGitProjectSource(root).materialize(pin, lease)
    write_into_checkout(
        lease_directory, {"written-after-materialize.txt": "from the lease\n"}
    )

    outcome = runner_for(root).run(pin, lease)

    assert _outcome_facts(outcome) == (
        ("/bin/cat", "written-after-materialize.txt"),
        0,
        Sha256Hash.of(b"from the lease\n"),
    )
    assert outcome.output_tail == b"from the lease\n"
    assert outcome.summary_line is None
    assert outcome.duration_seconds >= 0


@pytest.mark.proves("what-a-project-declares-and-where-it-runs-are-one-commit")
def test_overwriting_the_lease_manifest_does_not_change_the_command_that_runs(
    tmp_path: Path,
) -> None:
    """The pin owns the command; a lease-side overwrite of the manifest is not heard."""

    root = tmp_path / "project"
    pin = git_project(
        root, declaring_verification(["/bin/sh", "-c", "printf from-the-pin"])
    )
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a4" * 32), lease_directory)
    LocalGitProjectSource(root).materialize(pin, lease)
    write_into_checkout(
        lease_directory,
        declaring_verification(["/bin/sh", "-c", "printf from-the-lease"]),
    )

    outcome = runner_for(root).run(pin, lease)

    assert _outcome_facts(outcome) == (
        ("/bin/sh", "-c", "printf from-the-pin"),
        0,
        Sha256Hash.of(b"from-the-pin"),
    )
    assert outcome.output_tail == b"from-the-pin"


def test_the_declared_command_runs_in_the_lease_and_answers_with_its_own_outcome(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(
        root, declaring_verification(["/bin/sh", "-c", "pwd; printf ' works'; exit 7"])
    )
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a1" * 32), lease_directory)

    outcome = runner_for(root).run(pin, lease)

    assert _outcome_facts(outcome) == (
        ("/bin/sh", "-c", "pwd; printf ' works'; exit 7"),
        7,
        Sha256Hash.of(f"{lease_directory}\n works".encode()),
    )
    assert outcome.output_tail == f"{lease_directory}\n works".encode()


def _printing_exactly(byte_count: int) -> list[str]:
    """A verification whose whole output is a known number of bytes."""

    return ["/bin/sh", "-c", f"printf 'x%.0s' $(seq {byte_count})"]


def test_a_verification_printing_exactly_its_bound_still_answers(
    tmp_path: Path,
) -> None:
    """The widest output this adapter accepts is accepted, and hashed exactly.

    Pinned at the limit rather than near it, because a bound is only known to be
    the bound at the byte on either side of it. The digest is what the receipt
    keeps, so it is what this compares.
    """

    root = tmp_path / "project"
    pin = git_project(
        root,
        declaring_verification(_printing_exactly(MAXIMUM_VERIFICATION_OUTPUT_BYTES)),
    )
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("b1" * 32), lease_directory)

    outcome = runner_for(root).run(pin, lease)

    assert outcome.exit_code == 0
    assert outcome.standard_output_hash == Sha256Hash.of(
        b"x" * MAXIMUM_VERIFICATION_OUTPUT_BYTES
    )
    # The tail is a second, narrower record: not the whole answer the digest
    # above proves, only what this outcome retains to show a reader.
    assert len(outcome.output_tail) == MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES
    assert outcome.output_tail == b"x" * MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES


def test_a_verification_printing_one_byte_past_its_bound_is_refused(
    tmp_path: Path,
) -> None:
    """Past the bound the run is refused by name, not silently cut short.

    A truncated answer would hash to something no rerun of that command could
    reproduce, so the receipt would name a digest standing for nothing.
    """

    root = tmp_path / "project"
    pin = git_project(
        root,
        declaring_verification(
            _printing_exactly(MAXIMUM_VERIFICATION_OUTPUT_BYTES + 1)
        ),
    )
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("b2" * 32), lease_directory)

    with pytest.raises(
        ProjectVerificationUnavailable, match="did not answer"
    ) as raised:
        runner_for(root).run(pin, lease)

    # Named, so this cannot pass for the other reason the same refusal carries:
    # a command that ran out of time rather than out of room.
    assert str(MAXIMUM_VERIFICATION_OUTPUT_BYTES) in str(raised.value)


def test_the_verification_output_bound_is_this_adapter_s_own_number(
    tmp_path: Path,
) -> None:
    """What a verification may print does not follow a provider's wire format.

    The process port's ceiling is the largest frame any provider declares, and
    it rose when one provider's format grew to carry attempt transcripts (#666).
    A verification borrowing it would have been quietly widened by a change that
    had nothing to do with it.
    """

    del tmp_path
    assert (
        MAXIMUM_VERIFICATION_OUTPUT_BYTES < MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES
    )


def test_a_verification_past_its_declared_deadline_is_refused_rather_than_awaited(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/sh", "-c", "sleep 30"], 0.2))
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a2" * 32), lease_directory)

    with pytest.raises(
        ProjectVerificationUnavailable, match="did not answer"
    ) as raised:
        runner_for(root).run(pin, lease)

    assert raised.value.timeout_seconds == 0.2


@dataclass
class _RefusingStore:
    """A store that records what an attempt asked of it, and refuses to be claimed."""

    calls: list[str] = field(default_factory=list)

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        self.calls.append("prepare")
        return prepared_agent_attempt(execution)

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult:
        del execution
        self.calls.append("claim")
        raise AssertionError("a refused verification must not claim an attempt")

    def complete_success(self, *arguments: object) -> AgentAttemptSucceeded:
        raise AssertionError(arguments)


@dataclass
class _RefusingVerifications:
    """A runner standing for a project that states no verification."""

    asked: int = 0

    def preflight(self, pin: ProjectSourcePin) -> None:
        del pin
        self.asked += 1
        raise ProjectVerificationUndeclared("this project states no verification")

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        raise AssertionError((pin, lease))


@dataclass
class _UnlaunchedExecutor(PrintModeExecutor):
    """An executor whose command is prepared and whose process never starts."""

    launches: int = 0
    released: int = 0

    def prepare_process(self, request: object) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(("/bin/true",), standard_output_frame_bytes=1024)

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        self.launches += 1
        raise AssertionError((invocation, completion))

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command
        self.released += 1

    def close(self) -> None:
        return None


@dataclass
class _CountingWorkspaces:
    """A workspace owner that says whether an attempt ever reached its directory."""

    acquired: int = 0

    def preflight(self) -> None:
        return None

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        del attempt_id
        self.acquired += 1
        raise AssertionError("a refused verification must not lease a workspace")

    def release(self, attempt_id: AgentAttemptId) -> None:
        del attempt_id


@dataclass
class _RefusedAttempt:
    """One attempt driven until it refuses, and what it cost on the way."""

    store: _RefusingStore = field(default_factory=_RefusingStore)
    executor: _UnlaunchedExecutor = field(default_factory=_UnlaunchedExecutor)
    workspaces: _CountingWorkspaces = field(default_factory=_CountingWorkspaces)

    def drive(self, project: PinnedProjectSource) -> None:
        execute_agent_attempt(
            agent_attempt_execution(agent_execution_request_v2()),
            self.executor,  # type: ignore[arg-type]
            self.store,  # type: ignore[arg-type]
            _SilentSupervisor(),  # type: ignore[arg-type]
            self.workspaces,  # type: ignore[arg-type]
            project,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

    @property
    def cost(self) -> tuple[list[str], int, int]:
        """What the refusal spent: store calls, provider launches, leases taken."""

        return (self.store.calls, self.executor.launches, self.workspaces.acquired)


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
def test_an_undeclared_verification_refuses_before_the_attempt_is_claimed(
    tmp_path: Path,
) -> None:
    """The refusal costs nothing: no claim, no workspace, no provider process."""
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    verifications = _RefusingVerifications()
    attempt = _RefusedAttempt()

    with pytest.raises(ProjectVerificationUndeclared):
        attempt.drive(
            PinnedProjectSource(
                LocalGitProjectSource(root),
                verifications,
                CandidatesKeptInMemory(),
                pin,
                THE_GRANT,
                None,
            )
        )

    assert verifications.asked == 1
    assert attempt.cost == (["prepare"], 0, 0)
    assert attempt.executor.released == 1


@dataclass
class _TimeoutVerifications:
    """A runner that starts, then exceeds the deadline the project declared."""

    timeout_seconds: float
    ran: int = 0

    def preflight(self, pin: ProjectSourcePin) -> None:
        del pin

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        del pin, lease
        self.ran += 1
        raise ProjectVerificationUnavailable(
            f"the verification did not answer within its declared "
            f"{self.timeout_seconds} seconds",
            timeout_seconds=self.timeout_seconds,
        )


THE_ANSWER_THE_PROVIDER_GAVE = b'"I changed the file the check was about."'
"""What this provider answers, in the shape a receipt has to be able to show."""

DECODED_TRANSCRIPT = AttemptTranscript.of(
    [AssistantTurn("I changed the file the check was about.")]
)
"""What this provider decoded before the granted check stopped answering."""
TRANSCRIPT_RECORDED_AT = RecordedAt("2026-08-29T12:00:00Z")


@dataclass
class _ClaimingStore:
    """A store that wins the claim, then records how the attempt was ended."""

    calls: list[str] = field(default_factory=list)
    attempt: AgentAttempt | None = None
    verdict: str | None = None
    kept_transcript: AttemptTranscript | None = None

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        self.calls.append("prepare")
        self.attempt = prepared_agent_attempt(execution)
        return self.attempt

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult:
        del execution
        self.calls.append("claim")
        assert self.attempt is not None
        self.attempt = replace(
            self.attempt,
            state=AgentAttemptState.LAUNCH_ARMED,
            state_version=self.attempt.state_version + 1,
        )
        return AgentAttemptClaimedByThisCall(self.attempt)

    def complete_success(self, *arguments: object) -> AgentAttemptSucceeded:
        self.calls.append("complete_success")
        raise AssertionError(
            "a timed-out verification must not invent a redemption", arguments
        )

    def complete_project_verification_failure(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        del execution
        self.calls.append("fail")
        self.verdict = verdict
        self.kept_transcript = transcript
        assert self.attempt is not None
        self.attempt = replace(
            self.attempt,
            state=AgentAttemptState.FAILED,
            state_version=self.attempt.state_version + 1,
            failure_code=AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED,
        )
        return AgentAttemptFailed(self.attempt)


@dataclass
class _RecordingSuccessStore:
    """A store that wins the claim, then records exactly what `complete_success` got.

    Standing in for the durable store's own judgment of a redemption -- which
    `tests/integration/test_v3_tool_grant_run.py` proves against the real one --
    so this suite can pin what `execute_agent_attempt` computed and handed over,
    without a database.
    """

    calls: list[str] = field(default_factory=list)
    attempt: AgentAttempt | None = None
    redemption: ToolRedemptionReceipt | None = None
    verification_failure_evidence: ProjectVerificationFailureEvidence | None = None
    candidate_diff: str | None = None

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        self.calls.append("prepare")
        self.attempt = prepared_agent_attempt(execution)
        return self.attempt

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult:
        del execution
        self.calls.append("claim")
        assert self.attempt is not None
        self.attempt = replace(
            self.attempt,
            state=AgentAttemptState.LAUNCH_ARMED,
            state_version=self.attempt.state_version + 1,
        )
        return AgentAttemptClaimedByThisCall(self.attempt)

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: ToolRedemptionReceipt | None = None,
        verification_failure_evidence: ProjectVerificationFailureEvidence | None = None,
        candidate_diff: str | None = None,
    ) -> AgentAttemptFailed:
        del execution, result
        self.calls.append("complete_success")
        self.redemption = redemption
        self.verification_failure_evidence = verification_failure_evidence
        self.candidate_diff = candidate_diff
        assert self.attempt is not None
        self.attempt = replace(
            self.attempt,
            state=AgentAttemptState.FAILED,
            state_version=self.attempt.state_version + 1,
            failure_code=AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED,
        )
        return AgentAttemptFailed(self.attempt)


@dataclass
class _RecordingArtifactPublisher:
    """A publisher that keeps whatever it is asked to, and says it is new."""

    published: list[Artifact] = field(default_factory=list)

    def publish_artifact(self, artifact: Artifact) -> PublishArtifactResult:
        self.published.append(artifact)
        return ArtifactCreated(artifact)


@dataclass
class _UnavailableArtifactPublisher:
    """A publisher wired but unable to write, the way a database outage answers."""

    def publish_artifact(self, artifact: Artifact) -> PublishArtifactResult:
        del artifact
        return DurableWriteUnavailable()


@dataclass
class _SucceedingExecutor(PrintModeExecutor):
    """A provider that answers; the verification after it is the subject."""

    def prepare_process(self, request: object) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(("/bin/true",), standard_output_frame_bytes=1024)

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        del invocation, completion
        return AgentExecutionResult(THE_ANSWER_THE_PROVIDER_GAVE, DECODED_TRANSCRIPT)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


@dataclass
class _RecordingSupervisor:
    """A supervisor that records whether finalize ran after the claim.

    `leaves` is what the provider process wrote into its lease before it ended.
    A scenario that says nothing there is a provider that changed nothing, which
    is now an ending of its own -- so every scenario whose subject lies past the
    provider names the work it did.
    """

    finalized: int = 0
    leaves: Mapping[str, str] = field(default_factory=dict)

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        return prepared_agent_attempt(execution)

    def launch_and_wait(
        self,
        execution: AgentAttemptExecution,
        invocation: AgentProcessInvocation,
        permissions: PermissionDecider,
    ) -> AgentProcessCompletion:
        del permissions
        del execution
        write_into_checkout(invocation.lease.working_directory, self.leaves)
        return AgentProcessCompletion(0, b'"ok"', b"")

    def finalize(self, execution: AgentAttemptExecution) -> None:
        del execution
        self.finalized += 1


@dataclass
class _LeasingWorkspaces:
    """A workspace owner that records acquire and release after the claim."""

    directory: Path
    acquired: int = 0
    released: int = 0

    def preflight(self) -> None:
        return None

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        self.acquired += 1
        return leased_directory_identity(attempt_id, self.directory)

    def release(self, attempt_id: AgentAttemptId) -> None:
        del attempt_id
        self.released += 1


@pytest.mark.proves(
    "a-verification-timeout-after-claim-fails-the-attempt-durably-named"
)
def test_a_verification_that_times_out_after_claim_fails_the_attempt_named(
    tmp_path: Path,
) -> None:
    """A deadline after the claim is a named failure, not an armed leftover."""

    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    timeout_seconds = 0.2
    verifications = _TimeoutVerifications(timeout_seconds)
    store = _ClaimingStore()
    supervisor = _RecordingSupervisor()
    workspaces = _LeasingWorkspaces(tmp_path / "lease")

    outcome = execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _SucceedingExecutor(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        supervisor,  # type: ignore[arg-type]
        workspaces,  # type: ignore[arg-type]
        PinnedProjectSource(
            LocalGitProjectSource(root),
            verifications,
            CandidatesKeptInMemory(),
            pin,
            THE_GRANT,
            None,
        ),
        _RecordingArtifactPublisher(),  # type: ignore[arg-type]
        clock=lambda: TRANSCRIPT_RECORDED_AT,
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )

    assert isinstance(outcome, AgentAttemptFailed)
    assert outcome.attempt.state is AgentAttemptState.FAILED
    assert (
        outcome.attempt.failure_code
        is AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED
    )
    assert store.attempt is not None
    assert store.attempt.state is not AgentAttemptState.LAUNCH_ARMED
    assert store.calls == ["prepare", "claim", "fail"]
    assert store.verdict == f"timeout {timeout_seconds} seconds"
    # The provider had already answered when the check went silent, so what
    # it did reaches the ending rather than being dropped on this one path.
    expected_transcript = DECODED_TRANSCRIPT.with_recorded_moment(
        TRANSCRIPT_RECORDED_AT
    )
    assert store.kept_transcript == expected_transcript
    assert all(
        isinstance(event.moment, TranscriptRecordedMoment)
        and event.moment.origin is TranscriptMomentOrigin.RECORDED
        for event in expected_transcript.events
    )
    assert verifications.ran == 1
    assert workspaces.acquired == 1
    assert workspaces.released == 1
    assert supervisor.finalized == 1


FAILING_VERIFICATION_TAIL = (
    b"=================== 2 failed, 3 passed in 0.01s ===================\n"
)
FAILING_VERIFICATION_COMMAND = [
    "/bin/sh",
    "-c",
    f"printf '%s' '{FAILING_VERIFICATION_TAIL.decode('ascii')}'; exit 1",
]


WHAT_THE_BUILDER_CHANGED = "src/tool.py"
THE_BUILDERS_CHANGE = "print('the builder rewrote this')\n"
A_PROJECT_THE_BUILDER_CHANGES = {WHAT_THE_BUILDER_CHANGED: "print('as pinned')\n"}


def _drive_through_a_real_failing_verification(
    tmp_path: Path,
    artifacts: _RecordingArtifactPublisher | _UnavailableArtifactPublisher | None,
    store: _RecordingSuccessStore,
    command: list[str] | None = None,
    candidates: CandidateTreeStore | None = None,
) -> None:
    """A red check run by the real adapter, driven into the given store."""

    root = tmp_path / "project"
    pin = git_project(
        root,
        {
            **declaring_verification(command or FAILING_VERIFICATION_COMMAND),
            **A_PROJECT_THE_BUILDER_CHANGES,
        },
    )
    execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _SucceedingExecutor(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _RecordingSupervisor(  # type: ignore[arg-type]
            leaves={WHAT_THE_BUILDER_CHANGED: THE_BUILDERS_CHANGE}
        ),
        _LeasingWorkspaces(tmp_path / "lease"),
        PinnedProjectSource(
            LocalGitProjectSource(root),
            runner_for(root),
            candidates or CandidatesKeptInMemory(),
            pin,
            THE_GRANT,
            None,
        ),
        artifacts,  # type: ignore[arg-type]
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_grant_with_no_artifact_publisher_refuses_at_preflight(
    tmp_path: Path,
) -> None:
    """A runtime that can redeem a grant must be able to keep what a red check said.

    Refused before the claim and before any provider process -- not discovered
    only once a check has already exited nonzero. Silently dropping the
    check's output back to six words -- `exit 1` -- is exactly the loss
    #1137 exists to close, so a runtime wired without a place to keep it fails
    loud rather than reproducing that loss quietly.
    """
    store = _RecordingSuccessStore()

    with pytest.raises(RuntimeError, match="artifact publisher"):
        _drive_through_a_real_failing_verification(tmp_path, None, store)

    assert store.calls == ["prepare"]


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_nonzero_verification_publishes_its_tail_and_names_it_in_the_evidence(
    tmp_path: Path,
) -> None:
    """The store is handed the summary line and the address the tail was kept at."""

    publisher = _RecordingArtifactPublisher()
    store = _RecordingSuccessStore()

    _drive_through_a_real_failing_verification(tmp_path, publisher, store)

    assert store.calls == ["prepare", "claim", "complete_success"]
    evidence = store.verification_failure_evidence
    assert isinstance(evidence, ProjectVerificationFailureEvidence)
    assert evidence.summary_line == "2 failed, 3 passed in 0.01s"
    assert evidence.duration_seconds >= 0
    assert evidence.output.redacted is False
    assert evidence.output.retention_failure is None
    assert len(publisher.published) == 1
    published = publisher.published[0]
    assert published.content == FAILING_VERIFICATION_TAIL
    assert evidence.output.artifact_hash == published.artifact_hash


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_check_that_cannot_be_kept_degrades_the_words_instead_of_abandoning_the_attempt(
    tmp_path: Path,
) -> None:
    """A publisher that answers but cannot write still ends the attempt named.

    Preflight only catches a publisher this runtime was never wired with; a
    wired publisher that fails once the check has already run must not turn
    a `LAUNCH_ARMED` attempt no replay can resolve. The exit code, command and
    summary line still reach the receipt, with a note of their own for why the
    tail itself is not kept beside them.
    """
    store = _RecordingSuccessStore()

    _drive_through_a_real_failing_verification(
        tmp_path, _UnavailableArtifactPublisher(), store
    )

    assert store.calls == ["prepare", "claim", "complete_success"]
    evidence = store.verification_failure_evidence
    assert isinstance(evidence, ProjectVerificationFailureEvidence)
    assert evidence.summary_line == "2 failed, 3 passed in 0.01s"
    assert evidence.output.artifact_hash is None
    assert evidence.output.retention_failure is not None


FAILING_VERIFICATION_WITH_A_CREDENTIAL_COMMAND = [
    "/bin/sh",
    "-c",
    (
        "printf 'token: sk-ant-abcdefghijklmnopqrstuvwx\\n"
        "2 failed, 3 passed in 0.01s\\n'; exit 1"
    ),
]


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_credential_shape_in_a_red_checks_output_is_redacted_before_it_is_kept(
    tmp_path: Path,
) -> None:
    """A token a project's own tooling printed does not become HTTP-readable material."""

    publisher = _RecordingArtifactPublisher()
    store = _RecordingSuccessStore()

    _drive_through_a_real_failing_verification(
        tmp_path,
        publisher,
        store,
        command=FAILING_VERIFICATION_WITH_A_CREDENTIAL_COMMAND,
    )

    evidence = store.verification_failure_evidence
    assert isinstance(evidence, ProjectVerificationFailureEvidence)
    assert evidence.output.redacted is True
    published = publisher.published[0]
    assert b"sk-ant-" not in published.content
    assert REDACTION_MARKER.encode() in published.content


A_KEY_BLOCK_OPENS = assembled("-----BEGIN ", "RSA PRIVATE KEY", "-----")
A_KEY_BLOCK_CLOSES = assembled("-----END ", "RSA PRIVATE KEY", "-----")
KEY_MATERIAL_CHARACTER = "k"
"""One character of key material, and none of what stands where a key is taken out."""

FAILING_VERIFICATION_PRINTING_A_KEY_WIDER_THAN_THE_TAIL_COMMAND = [
    "/bin/sh",
    "-c",
    (
        f"printf '%s' '{A_KEY_BLOCK_OPENS}'; "
        f"printf '{KEY_MATERIAL_CHARACTER}%.0s' "
        f"$(seq {MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES}); "
        f"printf '%s' '{A_KEY_BLOCK_CLOSES}'; exit 1"
    ),
]
"""A check printing key material as wide as the whole tail an outcome retains."""


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_key_block_whose_opening_the_tail_cut_dropped_leaves_no_material_behind(
    tmp_path: Path,
) -> None:
    """The tail keeps the last bytes, so a wide enough key loses its opening marker.

    What is retained then is key material and the closing marker naming it, and
    no shape recognises a block whose opening is on the other side of the cut.
    A close standing without an opening before it says everything printed ahead
    of it was key material, so that is what goes.
    """

    publisher = _RecordingArtifactPublisher()
    store = _RecordingSuccessStore()

    _drive_through_a_real_failing_verification(
        tmp_path,
        publisher,
        store,
        command=FAILING_VERIFICATION_PRINTING_A_KEY_WIDER_THAN_THE_TAIL_COMMAND,
    )

    evidence = store.verification_failure_evidence
    assert isinstance(evidence, ProjectVerificationFailureEvidence)
    assert evidence.output.redacted is True
    published = publisher.published[0]
    assert KEY_MATERIAL_CHARACTER.encode() not in published.content
    assert b"PRIVATE KEY" not in published.content
    assert published.content.startswith(REDACTION_MARKER.encode())


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_zero_exit_verification_never_publishes_an_artifact(
    tmp_path: Path,
) -> None:
    """Nothing is kept for a check that passed: the outcome alone is the proof."""

    root = tmp_path / "project"
    pin = git_project(
        root, declaring_verification(["/bin/sh", "-c", "printf all-green"])
    )
    store = _RecordingSuccessStore()
    publisher = _RecordingArtifactPublisher()

    execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _SucceedingExecutor(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _RecordingSupervisor(),  # type: ignore[arg-type]
        _LeasingWorkspaces(tmp_path / "lease"),
        PinnedProjectSource(
            LocalGitProjectSource(root),
            runner_for(root),
            CandidatesKeptInMemory(),
            pin,
            THE_GRANT,
            None,
        ),
        publisher,  # type: ignore[arg-type]
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )

    assert publisher.published == []
    assert store.verification_failure_evidence is None


def _a_real_candidate_store(tmp_path: Path, root: Path) -> GitCandidateTreeStore:
    """The project's own store, where this runtime would really keep its work.

    Built from the two paths the product derives it from -- the checkout it is
    about and the database whose root it lives beside -- so what these tests ask
    it is what a run asks it.
    """

    database_path = tmp_path / "runtime" / "atelier.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return GitCandidateTreeStore(root, database_path)


@dataclass
class _RefusedVerifications:
    """A runner that fails the test if anything ever starts it."""

    def preflight(self, pin: ProjectSourcePin) -> None:
        del pin

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        raise AssertionError(
            "an attempt that changed nothing must not pay for a verification",
            pin,
            lease,
        )


@dataclass
class _UnchangedRecordingStore(_RecordingSuccessStore):
    """The claiming store, plus the one ending an untouched tree reaches."""

    verdict: str | None = None

    def complete_candidate_unchanged(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        del execution, transcript
        self.calls.append("complete_candidate_unchanged")
        self.verdict = verdict
        assert self.attempt is not None
        self.attempt = replace(
            self.attempt,
            state=AgentAttemptState.FAILED,
            state_version=self.attempt.state_version + 1,
            failure_code=AgentAttemptFailureCode.CANDIDATE_UNCHANGED,
        )
        return AgentAttemptFailed(self.attempt)


@pytest.mark.proves("an-attempt-that-changed-nothing-ends-before-it-pays-for-a-check")
def test_a_provider_that_left_the_pinned_tree_alone_ends_before_any_check_runs(
    tmp_path: Path,
) -> None:
    """The tree is read against the pin before a single second is spent on it.

    The real candidate store answers the question, so what is proved is the
    comparison a run really makes and not a fake's opinion of it. The verdict
    carries what the provider claimed beside the tree that contradicts it --
    the whole evidence #1156 exists to leave behind.
    """

    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    store = _UnchangedRecordingStore()

    outcome = execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _SucceedingExecutor(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _RecordingSupervisor(),  # type: ignore[arg-type]
        _LeasingWorkspaces(tmp_path / "lease"),
        PinnedProjectSource(
            LocalGitProjectSource(root),
            _RefusedVerifications(),  # type: ignore[arg-type]
            _a_real_candidate_store(tmp_path, root),
            pin,
            THE_GRANT,
            None,
        ),
        _RecordingArtifactPublisher(),  # type: ignore[arg-type]
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )

    assert isinstance(outcome, AgentAttemptFailed)
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.CANDIDATE_UNCHANGED
    assert store.calls == ["prepare", "claim", "complete_candidate_unchanged"]
    assert store.verdict is not None
    assert pin.tree in store.verdict
    assert THE_ANSWER_THE_PROVIDER_GAVE.decode("ascii") in store.verdict


@pytest.mark.proves("a-rejected-attempts-own-diff-is-kept-as-a-readable-artifact")
def test_a_red_check_keeps_the_patch_it_rejected_and_names_it_in_the_evidence(
    tmp_path: Path,
) -> None:
    """A check that said no is half an answer until a reader sees what it said no to.

    The patch is evidence, never a candidate: the work stays unkept, and what
    the attempt did stays readable. Its content is asked of the artifact the
    evidence names, so what is proved is the address a receipt would print.
    """

    root = tmp_path / "project"
    publisher = _RecordingArtifactPublisher()
    store = _RecordingSuccessStore()

    _drive_through_a_real_failing_verification(
        tmp_path,
        publisher,
        store,
        candidates=_a_real_candidate_store(tmp_path, root),
    )

    evidence = store.verification_failure_evidence
    assert isinstance(evidence, ProjectVerificationFailureEvidence)
    assert evidence.candidate_diff.retention_failure is None
    kept = {
        artifact.artifact_hash: artifact.content for artifact in publisher.published
    }
    assert evidence.candidate_diff.artifact_hash is not None
    patch = kept[evidence.candidate_diff.artifact_hash].decode("utf-8")
    assert WHAT_THE_BUILDER_CHANGED in patch
    assert THE_BUILDERS_CHANGE.strip() in patch


@pytest.mark.proves("a-rejected-attempts-own-diff-is-kept-as-a-readable-artifact")
def test_a_credential_shape_in_the_rejected_patch_is_redacted_before_it_is_kept(
    tmp_path: Path,
) -> None:
    """A token a builder wrote into a file does not become HTTP-readable material."""

    root = tmp_path / "project"
    pin = git_project(
        root,
        {
            **declaring_verification(FAILING_VERIFICATION_COMMAND),
            **A_PROJECT_THE_BUILDER_CHANGES,
        },
    )
    publisher = _RecordingArtifactPublisher()
    store = _RecordingSuccessStore()

    execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _SucceedingExecutor(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _RecordingSupervisor(  # type: ignore[arg-type]
            leaves={
                WHAT_THE_BUILDER_CHANGED: (
                    "TOKEN = 'sk-ant-abcdefghijklmnopqrstuvwx'\n"
                )
            }
        ),
        _LeasingWorkspaces(tmp_path / "lease"),
        PinnedProjectSource(
            LocalGitProjectSource(root),
            runner_for(root),
            _a_real_candidate_store(tmp_path, root),
            pin,
            THE_GRANT,
            None,
        ),
        publisher,  # type: ignore[arg-type]
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )

    evidence = store.verification_failure_evidence
    assert isinstance(evidence, ProjectVerificationFailureEvidence)
    assert evidence.candidate_diff.redacted is True
    kept = {
        artifact.artifact_hash: artifact.content for artifact in publisher.published
    }
    assert evidence.candidate_diff.artifact_hash is not None
    patch = kept[evidence.candidate_diff.artifact_hash]
    assert b"sk-ant-" not in patch
    assert REDACTION_MARKER.encode() in patch


PASSING_VERIFICATION_COMMAND = ["/bin/sh", "-c", "printf all-green"]
WHAT_THE_CHECK_WROTE = "the check itself wrote this line\n"
CHECK_THAT_WRITES_INTO_THE_WORKSPACE = [
    "/bin/sh",
    "-c",
    f"printf %s {json.dumps(WHAT_THE_CHECK_WROTE)} >> {WHAT_THE_BUILDER_CHANGED}",
]
"""A passing check that leaves the workspace different from how it found it."""


def _candidate_diff_handed_on_after_a_green_check(
    tmp_path: Path,
    what_the_builder_wrote: str,
    verification_command: list[str] | None = None,
) -> str | None:
    """What a passing attempt gives the node that judges its candidate next."""

    store = _RecordingSuccessStore()

    _drive_through_a_real_passing_verification(
        tmp_path, what_the_builder_wrote, store, verification_command
    )

    assert "complete_success" in store.calls
    return store.candidate_diff


def _drive_through_a_real_passing_verification(
    tmp_path: Path,
    what_the_builder_wrote: str,
    store: _RecordingSuccessStore,
    verification_command: list[str] | None = None,
) -> None:
    """A green check run by the real adapter, driven into the given store."""

    root = tmp_path / "project"
    pin = git_project(
        root,
        {
            **declaring_verification(
                verification_command or PASSING_VERIFICATION_COMMAND
            ),
            **A_PROJECT_THE_BUILDER_CHANGES,
        },
    )
    execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _SucceedingExecutor(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _RecordingSupervisor(  # type: ignore[arg-type]
            leaves={WHAT_THE_BUILDER_CHANGED: what_the_builder_wrote}
        ),
        _LeasingWorkspaces(tmp_path / "lease"),
        PinnedProjectSource(
            LocalGitProjectSource(root),
            runner_for(root),
            _a_real_candidate_store(tmp_path, root),
            pin,
            THE_GRANT,
            None,
        ),
        _RecordingArtifactPublisher(),  # type: ignore[arg-type]
        permissions=GRANTS_NOTHING,
        workspace_files=workspace_files_nobody_opens,
    )


def test_a_green_check_hands_on_the_patch_the_kept_candidate_is(
    tmp_path: Path,
) -> None:
    """A reviewer that reads no file still reads what this attempt did (#1235)."""

    diff = _candidate_diff_handed_on_after_a_green_check(tmp_path, THE_BUILDERS_CHANGE)

    assert diff is not None
    assert WHAT_THE_BUILDER_CHANGED in diff
    assert THE_BUILDERS_CHANGE.strip() in diff


def test_a_credential_shape_in_the_kept_patch_is_redacted_before_it_travels(
    tmp_path: Path,
) -> None:
    """The patch reaches another provider's job, so no token rides along in it."""

    diff = _candidate_diff_handed_on_after_a_green_check(
        tmp_path, "TOKEN = 'sk-ant-abcdefghijklmnopqrstuvwx'\n"
    )

    assert diff is not None
    assert "sk-ant-" not in diff
    assert REDACTION_MARKER in diff


def test_what_the_check_itself_wrote_is_in_the_patch_the_reviewer_reads(
    tmp_path: Path,
) -> None:
    """The check runs in the same workspace, so the tree it left is the candidate.

    A project may declare a command that formats, generates or fixes; whatever
    it writes is kept as part of the candidate, and a patch read before the
    check ran would show a reviewer a change nobody is about to open.
    """

    diff = _candidate_diff_handed_on_after_a_green_check(
        tmp_path, THE_BUILDERS_CHANGE, CHECK_THAT_WRITES_INTO_THE_WORKSPACE
    )

    assert diff is not None
    assert THE_BUILDERS_CHANGE.strip() in diff
    assert WHAT_THE_CHECK_WROTE.strip() in diff


PADDING_THAT_ENDS_JUST_BEFORE_THE_CUT = "a" * (MAXIMUM_CANDIDATE_DIFF_BYTES - 1_000)
"""Enough that what follows it begins before what a reader is shown ends."""

KEY_MATERIAL_SENTINEL = "KEYMATERIALKEYMATERIAL" * 64
"""Long enough that the cut falls inside it and its closing marker past it."""


def test_a_credential_lying_across_the_readers_cut_is_replaced_whole(
    tmp_path: Path,
) -> None:
    """Cutting first and scrubbing after would publish the half before the cut.

    The block below begins before the bound a reader is shown and ends past it,
    and only its closing marker makes it recognisable at all -- so a patch cut
    to that bound before the redactor sees it carries key material no shape can
    match any more.
    """

    diff = _candidate_diff_handed_on_after_a_green_check(
        tmp_path,
        f"{PADDING_THAT_ENDS_JUST_BEFORE_THE_CUT}\n"
        f"{A_KEY_BLOCK_OPENS}\n"
        f"{KEY_MATERIAL_SENTINEL}\n"
        f"{A_KEY_BLOCK_CLOSES}\n",
    )

    assert diff is not None
    assert KEY_MATERIAL_SENTINEL not in diff
    assert "RSA PRIVATE KEY" not in diff
    assert REDACTION_MARKER in diff
    assert len(diff.encode("utf-8")) <= MAXIMUM_CANDIDATE_DIFF_BYTES


@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_pin_this_source_cannot_answer_for_refuses_before_the_attempt_is_claimed(
    tmp_path: Path,
) -> None:
    """A tree nothing can unpack refuses by name rather than running on nothing."""
    root = tmp_path / "project"
    git_project(root, declaring_verification(["/bin/true"]))
    verifications = _RefusingVerifications()
    attempt = _RefusedAttempt()

    with pytest.raises(ProjectSourceUnavailable):
        attempt.drive(
            PinnedProjectSource(
                LocalGitProjectSource(root),
                verifications,
                CandidatesKeptInMemory(),
                A_PIN_NO_SOURCE_ANSWERS_FOR,
                THE_GRANT,
                None,
            )
        )

    assert verifications.asked == 0
    assert attempt.cost == (["prepare"], 0, 0)


class _SilentSupervisor:
    """A supervisor this scenario must never reach."""

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        raise AssertionError(execution)


AS_THE_PIN_HAS_IT = A_PROJECT_THE_BUILDER_CHANGES[WHAT_THE_BUILDER_CHANGED]
CHECK_THAT_REVERTS_THE_BUILDERS_CHANGE = [
    "/bin/sh",
    "-c",
    # `%b` rather than `%s`: the pinned line ends in a newline, and only the
    # escape-reading conversion writes one back out of a shell word.
    f"printf %b {json.dumps(AS_THE_PIN_HAS_IT)} > {WHAT_THE_BUILDER_CHANGED}",
]
"""A check that passes and leaves the workspace holding exactly the pinned tree."""


def test_a_green_check_that_undid_every_change_ends_as_a_candidate_unchanged(
    tmp_path: Path,
) -> None:
    """A tree read before the check is no answer to what the check left behind.

    A project may declare a command that reverts, cleans or regenerates; where
    it puts the workspace back exactly as the pin had it, there is no candidate
    to keep and no patch to hand on -- and a success naming neither would tell
    the node reading it that this attempt's change was empty rather than that
    it was undone.
    """

    store = _UnchangedRecordingStore()

    _drive_through_a_real_passing_verification(
        tmp_path,
        THE_BUILDERS_CHANGE,
        store,
        CHECK_THAT_REVERTS_THE_BUILDERS_CHANGE,
    )

    assert store.calls == ["prepare", "claim", "complete_candidate_unchanged"]
    assert store.candidate_diff is None
    assert store.verdict is not None
    assert THE_ANSWER_THE_PROVIDER_GAVE.decode("ascii") in store.verdict


WHAT_THE_RED_CHECK_WROTE = "the failing check itself wrote this line\n"
FAILING_CHECK_THAT_WRITES_INTO_THE_WORKSPACE = [
    "/bin/sh",
    "-c",
    (
        f"printf %s {json.dumps(WHAT_THE_RED_CHECK_WROTE)} "
        f">> {WHAT_THE_BUILDER_CHANGED}; "
        f"printf '%s' '{FAILING_VERIFICATION_TAIL.decode('ascii')}'; exit 1"
    ),
]
"""A check that writes into the workspace and then says no to what stands there."""


@pytest.mark.proves("a-rejected-attempts-own-diff-is-kept-as-a-readable-artifact")
def test_the_patch_a_red_check_rejected_is_the_tree_that_check_left(
    tmp_path: Path,
) -> None:
    """What an operator is shown is what the check said no to, not what preceded it.

    A check runs in the same workspace and may write into it before it exits
    nonzero -- a formatter, a generator, a fixer that got half way. A patch read
    before it ran shows a tree no check ever judged.
    """

    root = tmp_path / "project"
    publisher = _RecordingArtifactPublisher()
    store = _RecordingSuccessStore()

    _drive_through_a_real_failing_verification(
        tmp_path,
        publisher,
        store,
        FAILING_CHECK_THAT_WRITES_INTO_THE_WORKSPACE,
        candidates=_a_real_candidate_store(tmp_path, root),
    )

    evidence = store.verification_failure_evidence
    assert isinstance(evidence, ProjectVerificationFailureEvidence)
    assert evidence.candidate_diff.artifact_hash is not None
    kept = {
        artifact.artifact_hash: artifact.content for artifact in publisher.published
    }
    patch = kept[evidence.candidate_diff.artifact_hash].decode("utf-8")
    assert THE_BUILDERS_CHANGE.strip() in patch
    assert WHAT_THE_RED_CHECK_WROTE.strip() in patch


A_LINE_THE_BUILDER_REPEATS = "print('one more line of it')\n"
MORE_WORK_THAN_ANY_READER_IS_SHOWN = A_LINE_THE_BUILDER_REPEATS * (
    CANDIDATE_DIFF_READ_BYTES // len(A_LINE_THE_BUILDER_REPEATS) + 1
)
"""A change whose patch outgrows even what the store reads under its own bound."""


def test_a_patch_larger_than_the_store_reads_reaches_the_reviewer_saying_so(
    tmp_path: Path,
) -> None:
    """A cut nobody is told about reads as a change that ended where it stopped.

    The store stops reading at its own bound, and redaction can shrink what it
    read back under what a reader is shown -- so the length of the text is no
    answer to whether hunks were left out, and the marker is the only one.
    """

    diff = _candidate_diff_handed_on_after_a_green_check(
        tmp_path, MORE_WORK_THAN_ANY_READER_IS_SHOWN
    )

    assert diff is not None
    assert diff.endswith(CANDIDATE_DIFF_TRUNCATION_MARKER)
    assert len(diff.encode("utf-8")) <= MAXIMUM_CANDIDATE_DIFF_BYTES
    assert A_LINE_THE_BUILDER_REPEATS.strip() in diff
