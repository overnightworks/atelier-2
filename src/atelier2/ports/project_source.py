"""One project's own source, pinned to a commit and unpacked where the work happens.

An attempt does not work on "the project as it stands". It works on the exact tree
one commit names, resolved once when the node's durable binding is composed and
never resolved again -- so an operator may commit, rebase or check out anything
while a run is in flight without that run changing under it.

This port is how an attempt reaches that tree: pin the source, refuse a pin the
source can no longer answer for, read one declaration out of the pinned tree
without unpacking it, and unpack the tree into the directory the attempt leased.
What is unpacked is material, not a repository: no history travels with it, so
nothing in the lease can commit or fetch. That is a stated limit and not
isolation -- the lease's own sentence about that still stands.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Protocol

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
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> None:
        """Unpack the pinned tree into the directory this attempt leased."""
        ...
