"""What the builder node's claim precondition does with the ledger's answers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from atelier2.adapters.agent_claim_cli import AgentClaimCli
from atelier2.adapters.dbos.work_item_claims import (
    ConfirmedWorkItemClaim,
    WorkItemClaimHeld,
    WorkItemClaimLedger,
    WorkItemClaimRefused,
    hold_prepared_claim,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    ClaimWorkItem,
    ClaimWorkItemReceipt,
    HeadBranch,
    work_item_claim_id,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAdapterBinding,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    LogicalEffectKey,
)
from atelier2.contracts.executions import AgentExecutionRefusal
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.work_item_claims import (
    ClaimAbsent,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimTouch,
)
from tests.scenarios.work_item_claims import (
    FakeWorkItemClaims,
    claimed_ledger,
    fake_agent_claim_executable,
)

ITEM = 1320
RUN_ID = RunId("run-claim-before-build")
REVISION = WorkflowRevisionHash("c" * 64)
BRANCH = HeadBranch("atelier2/work-item/claim-before-build")
SCOPE = ("src/atelier2/adapters/dbos/work_item_claims.py", "tests")
CLAIM_ID = work_item_claim_id(RUN_ID, ITEM)
AGENT = f"atelier2 run {RUN_ID.value}"
LEDGER_BINDING = EffectAdapterBinding(
    AdapterRevision("agent-claim-cli/0.12.0"),
    EffectDestination("/checkout"),
    AdapterOperationalIdentity("/usr/bin/agent-claim"),
    AdapterOperationName.CLAIM_WORK_ITEM,
)


def _intent() -> EffectIntent:
    request = ClaimWorkItem(ITEM, CLAIM_ID, BRANCH, SCOPE)
    return EffectIntent(
        EffectBinding(
            LogicalEffectKey("atelier2-work-item-claim-test"),
            RUN_ID,
            REVISION,
            LEDGER_BINDING.adapter_revision,
            LEDGER_BINDING.destination,
            LEDGER_BINDING.operational_identity,
            AdapterOperationName.CLAIM_WORK_ITEM,
        ),
        CanonicalRequest(request.canonical_bytes()),
    )


def _receipt(
    *,
    branch: HeadBranch = BRANCH,
    scope: tuple[str, ...] = SCOPE,
    touches: tuple[ClaimTouch, ...] = (),
) -> ClaimReceipt:
    return ClaimReceipt(
        ITEM,
        CLAIM_ID,
        AGENT,
        branch,
        tuple(PurePosixPath(path) for path in scope),
        touches,
    )


def _held(claims: FakeWorkItemClaims) -> WorkItemClaimHeld | WorkItemClaimRefused:
    return hold_prepared_claim(_intent(), WorkItemClaimLedger(claims, LEDGER_BINDING))


def test_the_claim_asks_the_ledger_for_this_run_item_branch_and_scope() -> None:
    """The claim carries the run's identity and the item's own paths, and no more."""

    claims = FakeWorkItemClaims(claim_answer=_receipt())

    outcome = _held(claims)

    assert claims.read_back_requests == [(ITEM, CLAIM_ID)]
    assert [
        (
            request.item,
            request.agent,
            request.branch,
            request.scope,
            request.claim_id,
            request.out_of_order_reason,
        )
        for request in claims.claim_requests
    ] == [
        (
            ITEM,
            RUN_ID,
            BRANCH,
            tuple(PurePosixPath(path) for path in SCOPE),
            CLAIM_ID,
            None,
        )
    ]
    assert outcome == WorkItemClaimHeld(
        ConfirmedWorkItemClaim(
            ClaimWorkItemReceipt(ITEM, CLAIM_ID, AGENT, BRANCH, SCOPE),
            ConfirmationSource.ADAPTER_EXECUTION,
        )
    )


def test_a_claim_this_run_already_holds_is_read_back_and_never_taken_twice() -> None:
    """The retry path: the ledger's own claim answers, and no command mutates it.

    The receipt then says a read established it, never a command that ran.
    """

    claims = FakeWorkItemClaims(read_back_answer=_receipt())

    outcome = _held(claims)

    assert claims.claim_requests == []
    assert outcome == WorkItemClaimHeld(
        ConfirmedWorkItemClaim(
            ClaimWorkItemReceipt(ITEM, CLAIM_ID, AGENT, BRANCH, SCOPE),
            ConfirmationSource.ADAPTER_READBACK,
        )
    )


@pytest.mark.parametrize(
    ("reason", "refusal"),
    (
        (
            ClaimRefusalReason.PRIORITY,
            AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED_BY_PRIORITY,
        ),
        (
            ClaimRefusalReason.LEDGER_UNREADABLE,
            AgentExecutionRefusal.WORK_ITEM_CLAIM_LEDGER_UNREADABLE,
        ),
        (
            ClaimRefusalReason.UNKNOWN,
            AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED,
        ),
    ),
)
def test_every_ledger_refusal_ends_the_node_under_its_own_word(
    reason: ClaimRefusalReason, refusal: AgentExecutionRefusal
) -> None:
    claims = FakeWorkItemClaims(claim_answer=ClaimRefusal(reason))

    assert _held(claims) == WorkItemClaimRefused(refusal)


def test_an_unreadable_ledger_refuses_before_any_claim_is_attempted() -> None:
    claims = FakeWorkItemClaims(
        read_back_answer=ClaimRefusal(ClaimRefusalReason.LEDGER_UNREADABLE)
    )

    outcome = _held(claims)

    assert claims.claim_requests == []
    assert outcome == WorkItemClaimRefused(
        AgentExecutionRefusal.WORK_ITEM_CLAIM_LEDGER_UNREADABLE
    )


@pytest.mark.parametrize(
    "answer",
    (
        _receipt(branch=HeadBranch("atelier2/work-item/another")),
        _receipt(scope=("src/atelier2",)),
    ),
    ids=("another-branch", "another-scope"),
)
def test_a_claim_the_ledger_recorded_differently_refuses(answer: ClaimReceipt) -> None:
    """A run may build only under the claim it asked for, not under a neighbour.

    The grant exists all the same, so it is receipted as the ledger recorded
    it: a refusal that dropped it would leave a claim nothing can release.
    """

    claims = FakeWorkItemClaims(claim_answer=answer)

    outcome = _held(claims)

    assert isinstance(outcome, WorkItemClaimRefused)
    assert outcome.reason is AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED
    assert outcome.confirmed is not None
    assert outcome.confirmed.receipt.claim_id == CLAIM_ID
    assert (
        outcome.confirmed.receipt.branch,
        outcome.confirmed.receipt.claimed_scope,
    ) == (answer.branch, tuple(path.as_posix() for path in answer.claimed_scope))


def test_a_claim_touching_another_lane_refuses_and_keeps_its_receipt() -> None:
    """The claim happened, so it is recorded -- and the node still ends there."""

    touch = ClaimTouch(
        77, "other-claim", "atelier2 run other", (PurePosixPath("tests"),)
    )
    claims = FakeWorkItemClaims(claim_answer=_receipt(touches=(touch,)))

    outcome = _held(claims)

    assert isinstance(outcome, WorkItemClaimRefused)
    assert outcome.reason is AgentExecutionRefusal.WORK_ITEM_CLAIM_TOUCHES_ANOTHER_LANE
    assert outcome.confirmed is not None
    assert outcome.confirmed.receipt.touches[0].claim_id == "other-claim"
    assert outcome.confirmed.receipt.touches[0].scope == ("tests",)


def test_a_drive_that_died_before_its_receipt_takes_no_second_claim(
    tmp_path: Path,
) -> None:
    """Recovery re-runs the drive: the real command reads its own claim back.

    A workflow that died between the claim command and the durable receipt
    replays this drive, and the ledger already holds the claim the run's own
    claim id names. The proof runs the real command boundary against a ledger
    that refuses a second claim under one id, exactly as the tool does.
    """

    executable = fake_agent_claim_executable(tmp_path)
    ledger = WorkItemClaimLedger(AgentClaimCli(executable, tmp_path), LEDGER_BINDING)
    intent = _intent()

    first = hold_prepared_claim(intent, ledger)
    second = hold_prepared_claim(intent, ledger)

    receipt = ClaimWorkItemReceipt(ITEM, CLAIM_ID, AGENT, BRANCH, SCOPE)
    assert first == WorkItemClaimHeld(
        ConfirmedWorkItemClaim(receipt, ConfirmationSource.ADAPTER_EXECUTION)
    )
    assert second == WorkItemClaimHeld(
        ConfirmedWorkItemClaim(receipt, ConfirmationSource.ADAPTER_READBACK)
    )
    assert [request.claim_id for request in claimed_ledger(executable)] == [CLAIM_ID]


def test_a_ledger_holding_nothing_yet_is_claimed_once(tmp_path: Path) -> None:
    executable = fake_agent_claim_executable(tmp_path)
    claims = AgentClaimCli(executable, tmp_path)

    assert claims.read_back(ITEM, CLAIM_ID) == ClaimAbsent()
    assert isinstance(
        hold_prepared_claim(_intent(), WorkItemClaimLedger(claims, LEDGER_BINDING)),
        WorkItemClaimHeld,
    )
    assert claimed_ledger(executable)[0].scope == tuple(
        PurePosixPath(path) for path in SCOPE
    )
