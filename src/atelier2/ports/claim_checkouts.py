"""One checkout per run for the claim door, and nothing else runs in it.

A work-item claim is taken from a clean linked worktree on the lane branch: that
is the invariant the claim ledger checks before it reads or writes anything. The
provider's own lease is deliberately not that worktree -- it is material without
a repository, so nothing in it can commit, fetch or push -- and so the door owns
a checkout of its own: opened at the run's pin on the lane branch before the
claim is held, found again by the same run on every replay, and closed once the
claim is refused or released. The lane branch outlives the checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.runs import RunId


class ClaimCheckoutUnavailable(Exception):
    """No claim checkout could be made or removed, in the source's own words."""


class ClaimCheckoutRefused(Exception):
    """What stands at the run's path is not this run's claim checkout."""


class ClaimCheckouts(Protocol):
    """The provider-neutral owner of every run's claim checkout on this host."""

    def open(self, run_id: RunId, branch: HeadBranch, pin: ProjectSourcePin) -> Path:
        """The run's clean linked worktree on `branch` at `pin`: made, or found again."""
        ...

    def close(self, run_id: RunId) -> None:
        """Remove the run's checkout and its administration, keeping the lane branch."""
        ...
