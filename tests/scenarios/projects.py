"""Real git repositories to pin, read and unpack in tests.

A pin is a fact about a repository, so these scenarios build one rather than
imitating it: what a test asserts about a pinned tree is asserted against git
itself. Every repository is created under the test's own temporary directory and
reads no configuration of the machine it runs on -- the operator's global git
configuration could otherwise sign commits, run hooks or rename branches, and a
test that inherits it is a test that fails on somebody else's laptop.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.adapters.project_verification import PROJECT_MANIFEST_NAME
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.candidate_reports import ReadPatch
from atelier2.contracts.project_sources import (
    CandidateTree,
    GitObjectFormat,
    ProjectSourcePin,
)
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.candidate_store import (
    CandidateStoreUnavailable,
    LeasedWorkingTree,
)

COMMITTING_SCENARIO = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "scenario",
    "GIT_AUTHOR_EMAIL": "scenario@invalid",
    "GIT_COMMITTER_NAME": "scenario",
    "GIT_COMMITTER_EMAIL": "scenario@invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
"""Who commits and when, so a scenario's pins depend on its content alone."""


A_TREE_THE_PIN_DOES_NOT_NAME = "the-attempt-changed-something"
"""What the in-memory store says a lease holds where a test did not say.

Deliberately not shaped like an object name. The only question anyone asks of
this value is whether it equals the pin, and a plausible-looking hash would
invite a test to assert an address no repository ever produced.
"""


@dataclass
class CandidatesKeptInMemory:
    """A candidate store for tests whose subject is not the keeping itself.

    An attempt that cannot keep its work is a failed attempt, so every test
    driving one has to give it somewhere to keep it -- even when what that test
    is about is the grant, the process or the log. This answers the way the real
    store answers, reduced to what a caller can observe: the work stated as the
    tree it became, readable afterwards under the attempt that made it.

    The tree it states as *kept* is the pin's own, because a store inventing an
    object name would let a test assert an address no repository could ever
    produce. The tree it states as *standing in the lease* is not, because an
    attempt driven here did work: a test whose subject is an attempt that
    changed nothing drives the real store instead of saying so here.
    """

    kept: dict[AgentAttemptId, CandidateTree] = field(default_factory=dict)

    def capture(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> CandidateTree:
        candidate = CandidateTree(lease.attempt_id, pin.tree)
        self.kept[lease.attempt_id] = candidate
        return candidate

    def read(self, attempt_id: AgentAttemptId) -> CandidateTree | None:
        return self.kept.get(attempt_id)

    def attest(self, candidate: CandidateTree) -> None:
        if self.kept.get(candidate.attempt_id) != candidate:
            raise CandidateStoreUnavailable(f"nothing here keeps {candidate.tree}")

    def materialize(
        self, candidate: CandidateTree, lease: AgentAttemptWorkspaceLease
    ) -> None:
        """Unpack nothing: a scenario continuing real work drives the real store.

        Answering at all would have this fake decide what an attempt begins in,
        and a test asserting on that would be asserting on this file.
        """
        raise CandidateStoreUnavailable(
            f"{candidate.tree} cannot be unpacked into {lease.attempt_id.value} "
            "by a store that keeps no objects"
        )

    def written(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> LeasedWorkingTree:
        del lease
        return LeasedWorkingTree(pin, A_TREE_THE_PIN_DOES_NOT_NAME)

    def changes(self, written: LeasedWorkingTree) -> ReadPatch:
        del written
        return ReadPatch(b"", stopped_at_the_bound=False)


def git_project(
    root: Path,
    files: Mapping[str, str],
    object_format: GitObjectFormat = GitObjectFormat.SHA1,
) -> ProjectSourcePin:
    """A repository holding exactly these files at one commit, and the pin for it.

    The format is named because a repository's own decides how long every object
    name in it is, and a scenario that only ever builds SHA-1 repositories can
    say nothing about a project that chose the other one.
    """

    root.mkdir(parents=True, exist_ok=True)
    run_git(
        root,
        "init",
        "--quiet",
        "--initial-branch=main",
        f"--object-format={object_format.value}",
    )
    return commit_to_project(root, files)


def commit_to_project(root: Path, files: Mapping[str, str]) -> ProjectSourcePin:
    """Write these files into the checkout, commit them, and pin what results."""

    write_into_checkout(root, files)
    run_git(root, "add", "--all")
    run_git(root, "commit", "--quiet", "--message", "scenario")
    return LocalGitProjectSource(root).head()


def declared_in_checkout(root: Path, settings: Mapping[str, str]) -> None:
    """Write these settings into this repository's own `.git/config`.

    A project's local configuration is neither the machine's nor the product's:
    it travels with the checkout, and a scenario needs it to say what happens
    when a project declares something the product must not act on.
    """

    for name, value in settings.items():
        run_git(root, "config", name, value)


def write_into_checkout(root: Path, files: Mapping[str, str]) -> None:
    """Change the working copy alone, leaving every commit as it stands."""

    for name, body in files.items():
        written = root / name
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(body, encoding="utf-8")


def declaring_verification(
    command: list[str], timeout_seconds: float = 30
) -> Mapping[str, str]:
    """The one file a project needs to declare the command that verifies it."""

    return {
        PROJECT_MANIFEST_NAME: (
            "[tool.atelier2.verification]\n"
            f"command = {json.dumps(command)}\n"
            f"timeout_seconds = {timeout_seconds}\n"
        )
    }


def run_git(root: Path, *arguments: str) -> str:
    """Run one hookless git command against this repository, and read its stdout.

    Every git call this module makes, and every acceptance test that exercises
    a real push, funnels through this one runner rather than each spelling its
    own isolation -- a hook or identity leak from the operator's machine has
    exactly one place it could enter, and one place a test can catch it.
    """

    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        env={**os.environ, **COMMITTING_SCENARIO},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode().strip()
