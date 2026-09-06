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


class ClaimState(StrEnum):
    """What the ledger says about one branch."""

    UNCLAIMED = "UNCLAIMED"
    CLAIMED = "CLAIMED"
    CONFLICT = "CONFLICT"
    LEDGER_UNREADABLE = "LEDGER_UNREADABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ClaimTouch:
    """One active ledger claim whose scope touches a newly acquired claim."""

    item: int | None
    claim_id: str
    agent: str
    scope: tuple[PurePosixPath, ...]


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    """The ledger receipt for a claim the command acquired."""

    item: int
    claim_id: str
    branch: HeadBranch
    touches: tuple[ClaimTouch, ...]


@dataclass(frozen=True, slots=True)
class ClaimRefusal:
    """A claim command that completed without creating a usable receipt."""

    reason: ClaimRefusalReason


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
        out_of_order_reason: str | None,
    ) -> ClaimReceipt | ClaimRefusal: ...

    def status(self, branch: HeadBranch) -> ClaimState: ...

    def release(
        self, item: int, claim_id: str, outcome: ClaimReleaseOutcome
    ) -> ClaimRefusal | None: ...
