"""Claim checkouts as linked worktrees of the project's own checkout.

A linked worktree is the only checkout that satisfies the claim ledger without a
second clone: it shares the project's objects, remotes and trunk refs, and its
`git-dir` differs from the common one. It is made through the same isolated git
environment every project git call crosses -- no host configuration, no hooks --
and with sparse checkout switched off for the add, because a linked worktree
inherits the source's `core.sparseCheckout` and would silently hold less than
the pin. A `filter` driver in the source's own configuration is refused before
anything is made: its smudge would put content into the checkout the pin does
not carry.

What this leaves in the project checkout is worktree administration
(`.git/worktrees/<name>`) and the lane-branch ref. Every checkout is locked
for as long as it is open, so the prune that follows a `close` never takes
another run's administration -- the claim tool keeps its own state in the
worktree's git directory for as long as the claim stands -- and a directory
that vanished out of band keeps its entry until its own run closes it.

The root is attested as the agent scratch root is: no symbolic link, not
inside a git worktree, this process's own, mode 0700; and refused inside
the project working tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.agent_workspaces import (
    SCRATCH_ROOT_MODE,
    AgentScratchRootRefused,
    attested_directory,
    open_attested_root,
)
from atelier2.adapters.project_source import (
    GitRefused,
    answered_git,
    isolated_git_environment,
)
from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.runs import RunId
from atelier2.ports.claim_checkouts import (
    ClaimCheckoutRefused,
    ClaimCheckoutUnavailable,
)

_FILTER_DRIVER_CONFIGURATION_PREFIX = "filter."
_ABSOLUTE_GIT_DIRECTORIES = (
    "rev-parse",
    "--path-format=absolute",
    "--git-dir",
    "--git-common-dir",
)
_WHOLE_PIN_ARGUMENTS = ("-c", "core.sparseCheckout=false")
"""Prepended to the add so the checkout holds every path the pin carries."""
_WORKTREE_PATH_PREFIX = "worktree "
_WORKTREE_LOCKED_LINE = "locked"


class ClaimCheckoutRootRefused(ValueError):
    """The declared root cannot hold claim checkouts."""


@dataclass(frozen=True, slots=True)
class _RegisteredWorktree:
    path: str
    locked: bool


def _checkout_name(run_id: RunId) -> str:
    """A run id is a caller's string and may hold anything; its digest is a name."""

    return Sha256Hash.of(run_id.value.encode("utf-8")).value


def _registered_worktrees(listed: str) -> dict[str, _RegisteredWorktree]:
    """What `worktree list --porcelain` says stands registered, by recorded path."""

    registered: dict[str, _RegisteredWorktree] = {}
    for block in listed.split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith(_WORKTREE_PATH_PREFIX):
            continue
        path = lines[0].removeprefix(_WORKTREE_PATH_PREFIX)
        locked = any(
            line == _WORKTREE_LOCKED_LINE
            or line.startswith(f"{_WORKTREE_LOCKED_LINE} ")
            for line in lines[1:]
        )
        registered[path] = _RegisteredWorktree(path, locked)
    return registered


class LocalClaimCheckouts:
    """Every run's claim checkout under one root, made from one project checkout."""

    def __init__(self, project_checkout: Path, root: Path) -> None:
        self._project_checkout = project_checkout.resolve()
        absolute = Path(os.path.abspath(root))
        if absolute.resolve().is_relative_to(self._project_checkout):
            raise ClaimCheckoutRootRefused(
                f"the claim checkout root {root} lies inside the project checkout "
                f"{self._project_checkout}: a worktree inside the working tree it "
                "is linked to would be content of that tree"
            )
        try:
            self._root = attested_directory(absolute)
        except AgentScratchRootRefused as error:
            raise ClaimCheckoutRootRefused(str(error)) from error

    def open(self, run_id: RunId, branch: HeadBranch, pin: ProjectSourcePin) -> Path:
        path = self._root / _checkout_name(run_id)
        if path.is_symlink() or path.exists():
            self._refuse_unless_this_checkout(path, run_id, branch, pin)
            self._lock(path, run_id)
            return path
        self._refuse_filter_drivers()
        self._forget(path, run_id)
        self._root.mkdir(mode=SCRATCH_ROOT_MODE, parents=True, exist_ok=True)
        os.chmod(self._root, SCRATCH_ROOT_MODE)
        try:
            os.close(open_attested_root(self._root))
        except AgentScratchRootRefused as error:
            raise ClaimCheckoutUnavailable(str(error)) from error
        self._in_project(
            (
                *_WHOLE_PIN_ARGUMENTS,
                "worktree",
                "add",
                "--quiet",
                "-B",
                branch.value,
                str(path),
                pin.commit,
            ),
            failure=f"no claim checkout could be made for run {run_id.value} on "
            f"{branch.value} at {pin.commit}",
        )
        self._lock(path, run_id)
        return path

    def close(self, run_id: RunId) -> None:
        self._forget(self._root / _checkout_name(run_id), run_id)
        self._in_project(
            ("worktree", "prune"),
            failure=f"the worktree administration of {self._project_checkout} could "
            "not be pruned",
        )

    def _forget(self, path: Path, run_id: RunId) -> None:
        """Remove the run's registered worktree, standing or vanished; else nothing."""

        registered = self._registered().get(str(path))
        if registered is None:
            return
        failure = f"the claim checkout of run {run_id.value} could not be removed"
        if registered.locked:
            self._in_project(("worktree", "unlock", str(path)), failure=failure)
        self._in_project(("worktree", "remove", "--force", str(path)), failure=failure)

    def _lock(self, path: Path, run_id: RunId) -> None:
        registered = self._registered().get(str(path))
        if registered is None or registered.locked:
            return
        self._in_project(
            ("worktree", "lock", "--reason", run_id.value, str(path)),
            failure=f"the claim checkout of run {run_id.value} could not be locked",
        )

    def _registered(self) -> dict[str, _RegisteredWorktree]:
        return _registered_worktrees(
            self._answered(("worktree", "list", "--porcelain"))
        )

    def _refuse_unless_this_checkout(
        self, path: Path, run_id: RunId, branch: HeadBranch, pin: ProjectSourcePin
    ) -> None:
        """A standing path is found again only as the checkout it was opened as."""

        if path.is_symlink() or path.resolve() == self._project_checkout:
            raise ClaimCheckoutRefused(
                f"{path} is a link or the project checkout itself, not the claim "
                f"checkout of run {run_id.value}"
            )
        try:
            git_directory, common_directory = _answered_in(
                path, _ABSOLUTE_GIT_DIRECTORIES
            ).splitlines()
            standing = (
                _answered_in(path, ("branch", "--show-current")),
                _answered_in(path, ("rev-parse", "HEAD")),
            )
        except (GitRefused, ValueError) as error:
            raise ClaimCheckoutRefused(
                f"{path} stands where the claim checkout of run {run_id.value} "
                f"would be made and is no checkout of {self._project_checkout}: "
                f"{error}"
            ) from error
        if git_directory == common_directory or (
            common_directory
            != self._answered(_ABSOLUTE_GIT_DIRECTORIES).splitlines()[1]
        ):
            raise ClaimCheckoutRefused(
                f"{path} is not a linked worktree of {self._project_checkout}, so "
                f"it is not the claim checkout of run {run_id.value}"
            )
        if standing != (branch.value, pin.commit):
            raise ClaimCheckoutRefused(
                f"{path} is not the claim checkout of run {run_id.value} on "
                f"{branch.value} at {pin.commit}: it stands at {standing}"
            )

    def _refuse_filter_drivers(self) -> None:
        declared = sorted(
            {
                name
                for name in self._answered(
                    ("config", "--local", "--list", "--name-only")
                ).splitlines()
                if name.startswith(_FILTER_DRIVER_CONFIGURATION_PREFIX)
            }
        )
        if declared:
            raise ClaimCheckoutUnavailable(
                f"{self._project_checkout} declares the filter drivers "
                f"{', '.join(declared)}: a smudge would put content into the "
                "checkout that the pin does not carry, so no claim checkout is made"
            )

    def _answered(self, arguments: tuple[str, ...]) -> str:
        return self._in_project(
            arguments, failure=f"{self._project_checkout} could not be read"
        )

    def _in_project(self, arguments: tuple[str, ...], *, failure: str) -> str:
        try:
            return _answered_in(self._project_checkout, arguments)
        except GitRefused as error:
            raise ClaimCheckoutUnavailable(f"{failure}: {error}") from error


def _answered_in(directory: Path, arguments: tuple[str, ...]) -> str:
    return (
        answered_git(
            arguments,
            working_directory=str(directory),
            environment=isolated_git_environment(),
        )
        .decode("utf-8", "replace")
        .strip()
    )
