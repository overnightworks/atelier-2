"""One project's own source, pinned to a commit and unpacked where the work happens.

An attempt does not work on "the project as it stands". It works on the exact tree
one commit names, resolved once when the node's durable binding is composed and
never resolved again -- so an operator may commit, rebase or check out anything
while a run is in flight without that run changing under it.

This port is how an attempt reaches that tree: pin the source, refuse a pin the
source can no longer answer for, read one declaration out of the pinned tree
without unpacking it, check the tree out into the directory the attempt leased as
a linked worktree of the source -- on the lane branch a run claims from -- and
detach that lease again. Detached, the lease is material and not a repository:
nothing in it can commit, fetch or push. That is a stated limit and not isolation
-- the lease's own sentence about that still stands.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Protocol

from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease


class ProjectSourceUnavailable(Exception):
    """The pinned project source cannot be answered for, so nothing is claimed."""


class ProjectSourceRepository(Protocol):
    """The provider-neutral owner of one project's source and its pinned trees."""

    def head(self) -> ProjectSourcePin:
        """Pin the source as it stands now, so a later attempt runs on this tree."""
        ...

    def attest(self, pin: ProjectSourcePin) -> None:
        """Refuse a pin this source can no longer answer for, unpacking nothing."""
        ...

    def read(self, pin: ProjectSourcePin, path: PurePosixPath) -> bytes:
        """The bytes one file carries in the pinned tree, without unpacking it."""
        ...

    def materialize(
        self,
        pin: ProjectSourcePin,
        lease: AgentAttemptWorkspaceLease,
        branch: HeadBranch | None = None,
    ) -> None:
        """Check the pinned commit out into the leased directory as a linked
        worktree of this source: on `branch`, reset to the pin, or detached at it."""
        ...

    def detach_from_repository(self, lease: AgentAttemptWorkspaceLease) -> None:
        """Remove the worktree pointer from the lease and keep its tree untouched."""
        ...
