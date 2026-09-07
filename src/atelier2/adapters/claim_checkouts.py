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
(`.git/worktrees/<name>`) and the lane-branch ref. The administration is
removed only by `close`, never pruned on the side: the claim tool keeps its own
state in the worktree's git directory for as long as the claim stands.
"""

from __future__ import annotations

from pathlib import Path

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

_CHECKOUT_ROOT_MODE = 0o700
_FILTER_DRIVER_CONFIGURATION_PREFIX = "filter."
_ABSOLUTE_COMMON_DIRECTORY = ("rev-parse", "--path-format=absolute", "--git-common-dir")
_WHOLE_PIN_ARGUMENTS = ("-c", "core.sparseCheckout=false")
"""Prepended to the add so the checkout holds every path the pin carries."""


def _checkout_name(run_id: RunId) -> str:
    """A run id is a caller's string and may hold anything; its digest is a name."""

    return Sha256Hash.of(run_id.value.encode("utf-8")).value


class LocalClaimCheckouts:
    """Every run's claim checkout under one root, made from one project checkout."""

    def __init__(self, project_checkout: Path, root: Path) -> None:
        self._project_checkout = project_checkout.resolve()
        self._root = root.absolute()

    def open(self, run_id: RunId, branch: HeadBranch, pin: ProjectSourcePin) -> Path:
        path = self._root / _checkout_name(run_id)
        if path.exists():
            self._refuse_unless_this_checkout(path, run_id, branch, pin)
            return path
        self._refuse_filter_drivers()
        self._root.mkdir(mode=_CHECKOUT_ROOT_MODE, parents=True, exist_ok=True)
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
        return path

    def close(self, run_id: RunId) -> None:
        path = self._root / _checkout_name(run_id)
        if path.exists():
            self._in_project(
                ("worktree", "remove", "--force", str(path)),
                failure=f"the claim checkout of run {run_id.value} could not be removed",
            )
        self._in_project(
            ("worktree", "prune"),
            failure=f"the worktree administration of {self._project_checkout} could "
            "not be pruned",
        )

    def _refuse_unless_this_checkout(
        self, path: Path, run_id: RunId, branch: HeadBranch, pin: ProjectSourcePin
    ) -> None:
        """A standing directory is found again only as the checkout it was opened as."""

        try:
            standing = (
                _answered_in(path, _ABSOLUTE_COMMON_DIRECTORY),
                _answered_in(path, ("branch", "--show-current")),
                _answered_in(path, ("rev-parse", "HEAD")),
            )
        except GitRefused as error:
            raise ClaimCheckoutRefused(
                f"{path} stands where the claim checkout of run {run_id.value} "
                f"would be made and is no checkout of {self._project_checkout}: "
                f"{error}"
            ) from error
        expected = (self._common_directory(), branch.value, pin.commit)
        if standing != expected:
            raise ClaimCheckoutRefused(
                f"{path} is not the claim checkout of run {run_id.value} on "
                f"{branch.value} at {pin.commit}: it stands at {standing}"
            )

    def _common_directory(self) -> str:
        return self._answered(_ABSOLUTE_COMMON_DIRECTORY)

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
