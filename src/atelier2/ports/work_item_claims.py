"""The claim ledger boundary for an Atelier run's work item."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from atelier2.contracts.effect_requests import ClaimReasons, HeadBranch
from atelier2.contracts.runs import RunId
from atelier2.contracts.secret_redaction import redact_credentials

MAXIMUM_CLAIM_REFUSAL_DETAIL_BYTES = 512
"""How much of the ledger's own sentence a refusal keeps, in UTF-8 bytes."""


class ClaimRefusalReason(StrEnum):
    """The claim outcomes a caller may safely decide between."""

    PRIORITY = "priority"
    LEDGER_UNREADABLE = "ledger-unreadable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClaimTouch:
    """One active ledger claim: the lane holding it, and the scope it stands on."""

    item: int | None
    claim_id: str
    agent: str
    scope: tuple[PurePosixPath, ...]

    @property
    def lane(self) -> str:
        """How a refusal names this claim: its work item, or the agent without one."""

        return f"item {self.item}" if self.item is not None else self.agent


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
    """A claim command that completed without creating a usable receipt.

    `detail` is the one sentence the ledger gave for it -- the command's last
    `ERROR:` line, or the first failing check of a structured refusal -- kept
    verbatim so a reader learns the cause and not only its class, and empty
    where the ledger said nothing. It is credential-scrubbed and bounded here
    rather than at each call site, because an adapter that forgot either would
    carry a token into durable state.
    """

    reason: ClaimRefusalReason
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _bounded_detail(self.detail))


def _bounded_detail(said: str) -> str:
    """`said` without any credential shape, cut to the bytes a refusal keeps.

    Scrubbed before it is cut: a cut that split a token in half would leave a
    fragment no shape recognises.
    """

    scrubbed = redact_credentials(said).text.encode("utf-8")
    return scrubbed[:MAXIMUM_CLAIM_REFUSAL_DETAIL_BYTES].decode("utf-8", "ignore")


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
    """Acquire, inspect, and release the claim owned by one run.

    `checkout` is the run's claim checkout: the clean linked worktree on the
    lane branch the ledger reads before it acts, so a claim and its read-back
    are asked from there and never from the project checkout itself.
    """

    def claim(
        self,
        item: int,
        agent: RunId,
        branch: HeadBranch,
        scope: tuple[PurePosixPath, ...],
        claim_id: str,
        reasons: ClaimReasons,
        checkout: Path,
    ) -> ClaimReceipt | ClaimRefusal: ...

    def standing_claims(self) -> tuple[ClaimTouch, ...] | ClaimRefusal:
        """Every claim the ledger holds right now, taking none to find out.

        The occupancy question of a caller that holds no claim yet, and the
        only operation here that answers it: `claim` would have to take a claim
        to ask, and `read_back` answers only under a claim id such a caller
        does not have. The answer carries every lane, this caller's own
        included, because which one is its own is the lane identity the caller
        knows and this port does not.
        """
        ...

    def read_back(self, item: int, claim_id: str, checkout: Path) -> ClaimReadback:
        """What the ledger holds under this exact claim id, in full.

        A caller asks this before it would claim again: the claim id is minted
        from the run and its item, so a claim an earlier attempt already posted
        answers here with its own receipt instead of being taken a second time.
        """
        ...

    def release(
        self, item: int, agent: RunId, claim_id: str, outcome: ClaimReleaseOutcome
    ) -> ClaimRefusal | None: ...
