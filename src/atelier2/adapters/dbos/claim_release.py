"""Give back the claim of a run that has ended, once its landing is settled.

The claim door takes a work-item claim before a run's first build, and a
claim outlives the run that took it. This is where an ended run's claim is
given back, so the next lane finds the paths free. The sweep asks here which
ended run still holds its claim, and the answer is read rather than derived --
nothing about a landing is concluded from this store.

Two questions decide it, and each has an owner outside this module. Whether
anyone still reviews the run's work is the head branch's own answer, because a
pull request that is still open is work the next lane may not take the paths
of. Whether the work landed is the claim command's answer: it reads GitHub
itself before it releases -- the merge, the default branch, the pull request's
work-item line, and the item closed behind it -- so a release it grants is the
landing, and a release it refuses is never read here as one.

What this store contributes is only the pairing the run recorded: the claim it
holds, and the pull request it opened. The claim is given back under its own
claim id, never under its item number, because the same item may since have
been claimed by a person whose claim this sweep must not touch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import assert_never

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.schema import effect_receipts, runs
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import ClaimWorkItemReceipt
from atelier2.contracts.runs import TERMINAL_RUN_STATES, RunId
from atelier2.ports.effects import (
    HeadBranchPullRequests,
    HeadBranchPullRequestsUnreadable,
    NoPullRequestOpenOnHeadBranch,
    PullRequestOpenOnHeadBranch,
)
from atelier2.ports.work_item_claims import (
    Abandoned,
    ClaimRefusal,
    ClaimReleaseOutcome,
    Merged,
    WorkItemClaims,
)

_LOG = logging.getLogger("atelier2")

NO_PULL_REQUEST_RELEASE_REASON = (
    "der Lauf endete, ohne einen Pull Request zu hinterlassen"
)
"""Why a claim whose run published nothing to review is given back."""

UNLANDED_RELEASE_REASON = "kein Landing auf dem Standard-Branch nachgewiesen"
"""Why a claim whose pull request nobody reviews any more is given back."""

MAXIMUM_RELEASE_REASON_CHARACTERS = 512
"""How long a release reason may be, as the claim command bounds it."""


@dataclass(frozen=True, slots=True)
class _EndedRunClaim:
    """One ended run's own claim receipt, and the pull requests it published."""

    run_id: RunId
    claim: ClaimWorkItemReceipt
    pull_requests: frozenset[int]


def release_claims_of_ended_runs(
    engine: Engine,
    claims: WorkItemClaims,
    head_branch_pull_requests: HeadBranchPullRequests,
) -> None:
    """Resolve the claim of every run this store already ended.

    The ledger is read once for the whole pass, from the project checkout: a
    run's own claim checkout is removed when the run ends, so the claim id it
    left is looked for among the claims that stand, not read back through a
    checkout that is gone. A ledger that does not answer resolves nothing and
    says so, because a claim nobody could see is not a claim that is free.
    """

    standing = claims.standing_claims()
    if isinstance(standing, ClaimRefusal):
        _LOG.warning(
            "The claim ledger did not say which claims stand (%s); "
            "the claims of ended runs are resolved at the next tick.",
            standing.detail,
            extra={
                "event": "claim_release_ledger_unread",
                "detail": standing.detail,
            },
        )
        return
    items_by_claim_id = {claim.claim_id: claim.item for claim in standing}
    for ended in _ended_run_claims(engine):
        if _still_stands(ended, items_by_claim_id):
            _resolve(claims, head_branch_pull_requests, ended)


def _still_stands(
    ended: _EndedRunClaim, items_by_claim_id: dict[str, int | None]
) -> bool:
    """Whether exactly this run's claim is what the ledger holds today.

    A claim id the ledger no longer names is a claim someone already gave
    back, and this pass has nothing to do about it. One the ledger holds under
    another item is a contradiction this pass does not resolve by guessing.
    """

    claim_id = ended.claim.claim_id
    if claim_id not in items_by_claim_id:
        return False
    held_item = items_by_claim_id[claim_id]
    if held_item == ended.claim.item:
        return True
    _LOG.warning(
        "Claim %s stands on item %s, while run %s receipted it on item %s; "
        "it is left as it is.",
        claim_id,
        held_item,
        ended.run_id.value,
        ended.claim.item,
        extra={
            "event": "claim_release_identity_disagrees",
            "claim_id": claim_id,
            "run_id": ended.run_id.value,
        },
    )
    return False


def _resolve(
    claims: WorkItemClaims,
    head_branch_pull_requests: HeadBranchPullRequests,
    ended: _EndedRunClaim,
) -> None:
    """Give this claim back the way its run's pull request settled, or leave it.

    A run that published nothing left nothing to review or to land. A run that
    published more than one pull request leaves which of them settles the
    claim unanswered, and this pass answers no question the store did not.
    """

    if not ended.pull_requests:
        _release(claims, ended, Abandoned(NO_PULL_REQUEST_RELEASE_REASON))
        return
    if len(ended.pull_requests) > 1:
        _log_ambiguous_pull_requests(ended)
        return
    (pull_request,) = ended.pull_requests
    reviewing = head_branch_pull_requests.open_pull_requests_on(ended.claim.branch)
    match reviewing:
        case PullRequestOpenOnHeadBranch(number):
            _log_awaited_review(ended, number)
        case HeadBranchPullRequestsUnreadable(reason):
            _log_unread_review(ended, reason.detail)
        case NoPullRequestOpenOnHeadBranch():
            _release_settled_pull_request(claims, ended, pull_request)
        case unreachable:
            assert_never(unreachable)


def _log_ambiguous_pull_requests(ended: _EndedRunClaim) -> None:
    _LOG.warning(
        "Run %s published pull requests %s under one claim; its claim %s stands.",
        ended.run_id.value,
        ", ".join(f"#{number}" for number in sorted(ended.pull_requests)),
        ended.claim.claim_id,
        extra={
            "event": "claim_release_pull_request_ambiguous",
            "claim_id": ended.claim.claim_id,
            "run_id": ended.run_id.value,
        },
    )


def _log_awaited_review(ended: _EndedRunClaim, pull_request: int) -> None:
    _LOG.info(
        "Pull request #%s still stands open on %s; claim %s keeps its paths.",
        pull_request,
        ended.claim.branch.value,
        ended.claim.claim_id,
        extra={
            "event": "claim_release_awaits_review",
            "claim_id": ended.claim.claim_id,
            "pull_request": pull_request,
        },
    )


def _log_unread_review(ended: _EndedRunClaim, detail: str) -> None:
    _LOG.warning(
        "Nothing could be read about the pull requests on %s (%s); "
        "claim %s is resolved at the next tick.",
        ended.claim.branch.value,
        detail,
        ended.claim.claim_id,
        extra={
            "event": "claim_release_review_unread",
            "claim_id": ended.claim.claim_id,
            "detail": detail,
        },
    )


def _release_settled_pull_request(
    claims: WorkItemClaims, ended: _EndedRunClaim, pull_request: int
) -> None:
    """Release on the landing the ledger confirms, or as the abandonment it is.

    The ledger command is the one reader of the merge, so it is asked for the
    landing rather than told about one. What it refuses is a pull request
    nobody reviews any more whose landing it could not confirm, and its own
    sentence travels into the claim's record as the reason.
    """

    refusal = _release(claims, ended, Merged(pull_request))
    if refusal is None:
        return
    unlanded = _release(claims, ended, Abandoned(_unlanded_reason(refusal.detail)))
    if unlanded is not None:
        _log_unresolved(ended, unlanded.detail)


def _log_unresolved(ended: _EndedRunClaim, detail: str) -> None:
    """Say that a claim nothing could resolve is still standing, and why.

    Neither a landing nor an abandonment was accepted for it, so it stays
    where it is with its reason in the open: the next tick asks again, and a
    person can end it with the same command by hand.
    """

    _LOG.warning(
        "Claim %s of item %s could be released neither as landed nor as "
        "abandoned (%s); it stands until the next tick or a hand.",
        ended.claim.claim_id,
        ended.claim.item,
        detail,
        extra={
            "event": "claim_release_unresolved",
            "claim_id": ended.claim.claim_id,
            "detail": detail,
        },
    )


def _release(
    claims: WorkItemClaims, ended: _EndedRunClaim, outcome: ClaimReleaseOutcome
) -> ClaimRefusal | None:
    """Ask the ledger for this exact claim id, and say what it answered."""

    refusal = claims.release(
        ended.claim.item, ended.run_id, ended.claim.claim_id, outcome
    )
    if refusal is None:
        _LOG.info(
            "Claim %s of item %s is released as %s.",
            ended.claim.claim_id,
            ended.claim.item,
            outcome,
            extra={
                "event": "claim_released",
                "claim_id": ended.claim.claim_id,
                "item": ended.claim.item,
            },
        )
        return None
    _LOG.info(
        "The ledger did not release claim %s as %s (%s).",
        ended.claim.claim_id,
        outcome,
        refusal.detail,
        extra={
            "event": "claim_release_refused",
            "claim_id": ended.claim.claim_id,
            "detail": refusal.detail,
        },
    )
    return refusal


def _unlanded_reason(said: str) -> str:
    """The ledger's own sentence as a reason that same ledger accepts back.

    A reason is one bounded line there, while a refusal detail is whatever the
    command printed, so it is folded and cut here rather than refused there.
    """

    detail = " ".join(said.split())
    reason = (
        f"{UNLANDED_RELEASE_REASON}: {detail}" if detail else UNLANDED_RELEASE_REASON
    )
    return reason[:MAXIMUM_RELEASE_REASON_CHARACTERS]


def _ended_run_claims(engine: Engine) -> tuple[_EndedRunClaim, ...]:
    """Every claim receipt an ended run left, with the pull requests it opened.

    One claim id per ended run, however many of its nodes receipted it: they
    all name the one claim the run holds on its item, and it is given back
    once.
    """

    with engine.connect() as connection:
        receipted = connection.execute(
            sa.select(effect_receipts.c.run_id, effect_receipts.c.result)
            .join(runs, runs.c.run_id == effect_receipts.c.run_id)
            .where(
                effect_receipts.c.operation_name
                == AdapterOperationName.CLAIM_WORK_ITEM.value,
                runs.c.state.in_(tuple(state.value for state in TERMINAL_RUN_STATES)),
            )
        ).all()
        published = _published_pull_requests(
            connection, {str(record.run_id) for record in receipted}
        )
    ended: dict[str, _EndedRunClaim] = {}
    for record in receipted:
        run_id = str(record.run_id)
        claim = ClaimWorkItemReceipt.from_result_bytes(bytes(record.result))
        ended[claim.claim_id] = _EndedRunClaim(
            RunId(run_id), claim, published.get(run_id, frozenset())
        )
    return tuple(ended.values())


def _published_pull_requests(
    connection: Connection, run_ids: set[str]
) -> dict[str, frozenset[int]]:
    """Which pull requests each of these runs opened, by number.

    The number is what an `open-pr` adapter records as the effect's own id,
    because a pull request's identity at the destination is that number.
    """

    if not run_ids:
        return {}
    records = connection.execute(
        sa.select(effect_receipts.c.run_id, effect_receipts.c.effect_id).where(
            effect_receipts.c.operation_name == AdapterOperationName.OPEN_PR.value,
            effect_receipts.c.run_id.in_(run_ids),
        )
    )
    published: dict[str, set[int]] = {}
    for record in records:
        published.setdefault(str(record.run_id), set()).add(int(record.effect_id))
    return {run_id: frozenset(numbers) for run_id, numbers in published.items()}
