"""Recorded answers for application scenarios that need the claim-ledger port."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.runs import RunId
from atelier2.ports.work_item_claims import (
    ClaimReceipt,
    ClaimRefusal,
    ClaimReleaseOutcome,
    ClaimState,
)


@dataclass
class FakeWorkItemClaims:
    """Returns arranged port answers and records each request its caller made."""

    claim_answer: ClaimReceipt | ClaimRefusal | None = None
    status_answer: ClaimState = ClaimState.UNCLAIMED
    release_answer: ClaimRefusal | None = None
    claim_requests: list[
        tuple[int, RunId, HeadBranch, tuple[PurePosixPath, ...], str | None]
    ] = field(default_factory=list)
    status_requests: list[HeadBranch] = field(default_factory=list)
    release_requests: list[tuple[int, str, ClaimReleaseOutcome]] = field(
        default_factory=list
    )

    def claim(
        self,
        item: int,
        agent: RunId,
        branch: HeadBranch,
        scope: tuple[PurePosixPath, ...],
        out_of_order_reason: str | None,
    ) -> ClaimReceipt | ClaimRefusal:
        self.claim_requests.append((item, agent, branch, scope, out_of_order_reason))
        if self.claim_answer is None:
            raise AssertionError("this scenario did not arrange a claim answer")
        return self.claim_answer

    def status(self, branch: HeadBranch) -> ClaimState:
        self.status_requests.append(branch)
        return self.status_answer

    def release(
        self, item: int, claim_id: str, outcome: ClaimReleaseOutcome
    ) -> ClaimRefusal | None:
        self.release_requests.append((item, claim_id, outcome))
        return self.release_answer
