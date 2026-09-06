"""The claim ledger boundary for an Atelier run's work item."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.runs import RunId


class ClaimRefusalReason(StrEnum):
    """The claim outcomes a caller may safely decide between."""

    PRIORITY = "priority"
    LEDGER_UNREADABLE = "ledger-unreadable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClaimTouch:
    """One active ledger claim whose scope touches a newly acquired claim."""

    item: int | None
    claim_id: str
    agent: str
    scope: tuple[PurePosixPath, ...]


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    """The ledger receipt for a claim the command acquired.

    `claimed_scope` is what the ledger recorded for this claim, so a caller
    reads the ledger's own answer rather than assuming it took what was asked.
    `touches` are the foreign lanes standing on those paths, never this claim's
    own scope.
    """

    item: int
    claim_id: str
    agent: str
    branch: HeadBranch
    claimed_scope: tuple[PurePosixPath, ...]
    touches: tuple[ClaimTouch, ...]


@dataclass(frozen=True, slots=True)
class ClaimRefusal:
    """A claim command that completed without creating a usable receipt."""

    reason: ClaimRefusalReason


@dataclass(frozen=True, slots=True)
class ClaimAbsent:
    """The ledger was read, and it holds no claim under this identity."""


type ClaimReadback = ClaimReceipt | ClaimAbsent | ClaimRefusal


@dataclass(frozen=True, slots=True)
class Merged:
    """A claim released after its work item landed in this pull request."""

    pull_request: int


@dataclass(frozen=True, slots=True)
class Abandoned:
    """A claim released without its work item landing."""

    reason: str


type ClaimReleaseOutcome = Merged | Abandoned


class WorkItemClaims(Protocol):
    """Acquire, inspect, and release the claim owned by one run."""

    def claim(
        self,
        item: int,
        agent: RunId,
        branch: HeadBranch,
        scope: tuple[PurePosixPath, ...],
        claim_id: str,
        out_of_order_reason: str | None,
    ) -> ClaimReceipt | ClaimRefusal: ...

    def read_back(self, item: int, claim_id: str) -> ClaimReadback:
        """What the ledger holds under this exact claim id, in full.

        A caller asks this before it would claim again: the claim id is minted
        from the run and its item, so a claim an earlier attempt already posted
        answers here with its own receipt instead of being taken a second time.
        """
        ...

    def release(
        self, item: int, agent: RunId, claim_id: str, outcome: ClaimReleaseOutcome
    ) -> ClaimRefusal | None: ...
