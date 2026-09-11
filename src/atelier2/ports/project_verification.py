"""Running the verification a project declares for itself, in an attempt's lease.

The command belongs to the project, never to the agent and never to the atelier:
a node that redeems `run-project-verification` asks for *the project's* check, and
what that check is has one owner -- the project manifest. This port is how the
attempt reaches it.

Which manifest is asked is settled by the pin: the declaration is read out of the
tree one commit names. The command then runs in the lease after the provider has
worked there, so what a project declares and where it is run can be two different
trees -- the pin's command, the lease as it stands.

Where it runs is the attempt's decision, which is why `run` takes the lease: the
same split `AgentProcessCommand` already draws between a provider that owns its
command and an attempt that owns its place. The lease claims nothing about
operating-system isolation, and neither does this: the verification runs as this
process's own user, in the directory the attempt leased, with the deadline the
project declared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.candidate_store import CandidateTreeStore
from atelier2.ports.project_source import ProjectSourceRepository

MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES = 65_536
"""How much of a verification's stdout and stderr, combined, an outcome retains.

A red build's own words are the evidence an operator repairs from (#1137): the
exit code alone answers nothing about whether a test broke or the environment
did. Kept from the *end* of the combined streams, because a failing command's
own diagnosis -- a traceback, a pytest short summary -- is printed last, and 64
KiB is an operator-legible console tail rather than a derivation from
`MAXIMUM_VERIFICATION_OUTPUT_BYTES`, which merely bounds how much of the raw
answer this runtime will read at all before refusing it outright. What it costs
is one resident copy of that tail, carried from the adapter that ran the
command to whichever ending publishes or discards it.
"""

_VERDICT_COUNT = (
    r"\d+ (?:passed|failed|error(?:s)?|skipped|xfailed|xpassed|deselected|"
    r"warning(?:s)?)"
)
_VERDICT_LINE = re.compile(
    rf"(?:no tests ran|{_VERDICT_COUNT}(?:,\s*{_VERDICT_COUNT})*)(?:\s+in\s+.*)?"
)


def pytest_summary_line(output_tail: bytes) -> str | None:
    """The short summary pytest prints last, read from a retained tail.

    Scanned from the end, because pytest brackets several section headers the
    same way (`FAILURES`, `warnings summary`, `short test summary info`) and
    only the run's own verdict is one this reads. `pytest -q` -- the shape this
    runtime actually invokes -- prints that verdict bare, with no `=` border at
    all; only a plain run without `-q`, or one bracketed for a wider terminal,
    wraps it. Either way the verdict itself is never prose: it is `no tests
    ran`, or one or more `<count> <word>` groups pytest's own vocabulary
    produces, optionally followed by `in <duration>`. A bracketed section
    header such as `warnings summary` carries no such count and is never
    mistaken for one. A summary a long run pushed past the retained tail is
    not a summary this outcome can honestly claim to carry, so a tail with
    none answers `None` rather than guessing at an earlier section.
    """
    text = output_tail.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        # '=' padding around a pytest section title. A pattern of `=+`, `\s*`
        # and `.*?` backtracks cubically on a long unmatched line; verification
        # output is untrusted and unbounded.
        if len(stripped) >= 2 and stripped[0] == "=" and stripped[-1] == "=":
            content = stripped.strip("=").strip()
        else:
            content = stripped
        if _VERDICT_LINE.fullmatch(content) is not None:
            return content
    return None


class ProjectVerificationUndeclared(Exception):
    """The project this runtime was pointed at declares no verification command."""


class ProjectVerificationUnavailable(Exception):
    """The declared verification could not be run to an exit, so nothing is redeemed.

    `timeout_seconds` is the deadline the project declared when the command was
    started and then did not answer; it is absent when the command could not be
    started at all.
    """

    def __init__(self, message: str, *, timeout_seconds: float | None = None) -> None:
        super().__init__(message)
        self.timeout_seconds = timeout_seconds


@dataclass(frozen=True)
class ProjectVerificationOutcome:
    """What one declared verification ran, how it ended, and what it said.

    `standard_output_hash` is unchanged from before this outcome carried more:
    the digest of the full standard output this runtime read, up to
    `MAXIMUM_VERIFICATION_OUTPUT_BYTES`, kept for exactly the reason it always
    was -- proof that exactly this command produced exactly this answer.
    `output_tail` is a second, deliberately narrower record: the last
    `MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES` of *both* streams combined, retained
    so a reader can see what a red build actually said without rerunning it.
    `summary_line` is pytest's own short summary, read from that tail where one
    is there to read.
    """

    command: tuple[str, ...]
    exit_code: int
    standard_output_hash: Sha256Hash
    duration_seconds: float
    output_tail: bytes
    summary_line: str | None

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("a verification outcome names the command that ran")
        if self.duration_seconds < 0:
            raise ValueError("a verification cannot have run for negative time")
        if len(self.output_tail) > MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES:
            raise ValueError(
                f"a verification outcome retains at most "
                f"{MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES} bytes of output tail"
            )


class ProjectVerificationRunner(Protocol):
    """The provider-neutral owner of the project's own verification command."""

    def preflight(self, pin: ProjectSourcePin) -> None:
        """Refuse an undeclared verification without mutating or running anything."""
        ...

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        """Run the command the pinned tree declares, in this attempt's directory."""
        ...


@dataclass(frozen=True)
class PinnedProjectSource:
    """One attempt's project: the declared project, and the commit this attempt got.

    The pin is chosen once, when the node's durable binding is composed, so the
    tree the attempt unpacks and the manifest that declares its verification are
    one commit. The directory that verification runs in is that same lease after
    the provider has worked there, not a rematerialized pin tree. The grant a node
    pinned travels here rather than beside it because a grant without a project
    source is a promise nothing can keep -- there would be no manifest to read it
    from and no tree to run it in. Where a node pinned none, the grant is absent
    and the runner beside it is simply not asked.

    The store of candidates travels with them because what an attempt made is a
    change *to this pin*: the same value that says which tree the work started
    from says where the work it became is kept.

    Where the attempt *begins* is a second question, and this value is its one
    owner: a node continuing an earlier publication of its run begins in that
    publication's candidate, everyone else in the tree the pin names. The pin
    stays the comparison either way, so a continued attempt's patch is
    cumulative and a continuation that changes nothing is no failure.
    """

    source: ProjectSourceRepository
    verifications: ProjectVerificationRunner
    candidates: CandidateTreeStore
    pin: ProjectSourcePin
    grant: DeclaredToolGrant | None
    start_candidate: CandidateTree | None = None

    def attest(self) -> None:
        """Refuse what this attempt could not begin in, unpacking nothing.

        Asked before a work item is claimed, so a node that cannot begin never
        holds a lane. A start candidate that cannot be read refuses here rather
        than falling back on the pin: falling back would begin the continuation
        on a tree the earlier publication has already moved past, and its push
        would then take that earlier work off the branch.
        """

        self.source.attest(self.pin)
        if self.start_candidate is not None:
            self.candidates.attest(self.start_candidate)

    def materialize(self, lease: AgentAttemptWorkspaceLease) -> None:
        """Unpack what this attempt begins in into the directory it leased."""

        if self.start_candidate is None:
            self.source.materialize(self.pin, lease)
            return
        self.candidates.materialize(self.start_candidate, lease)


@dataclass(frozen=True)
class DeclaredProject:
    """One project: where its work comes from, what verifies it, and what keeps it.

    The three travel as one value because none of them answers alone: a source
    nothing verifies redeems no grant, a verification with no source has no tree
    to read its declaration from and none to run in, and a store of candidates
    with neither would keep work no pin says anything about. None of the three
    is optional either -- a project whose candidates had nowhere to go would run
    attempts whose work dies with their directory, and that is the loss this
    value exists to make unrepresentable.
    """

    source: ProjectSourceRepository
    verifications: ProjectVerificationRunner
    candidates: CandidateTreeStore

    def pinned(
        self,
        pin: ProjectSourcePin,
        grant: DeclaredToolGrant | None,
        start_candidate: CandidateTree | None = None,
    ) -> PinnedProjectSource:
        """What one attempt of this project works in, redeems in, and keeps."""

        return PinnedProjectSource(
            self.source,
            self.verifications,
            self.candidates,
            pin,
            grant,
            start_candidate,
        )
