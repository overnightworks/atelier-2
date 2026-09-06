"""The bytes a work-item claim travels as, and the identity it is taken under."""

from __future__ import annotations

import pytest

from atelier2.contracts.effect_requests import (
    ClaimedLanePath,
    ClaimWorkItem,
    ClaimWorkItemReceipt,
    HeadBranch,
    work_item_claim_id,
)
from atelier2.contracts.runs import RunId

RUN = RunId("run-claim-before-build")
BRANCH = HeadBranch("atelier2/work-item/claim-before-build")
CLAIM_ID = work_item_claim_id(RUN, 1320)


def _request() -> ClaimWorkItem:
    return ClaimWorkItem(1320, CLAIM_ID, BRANCH, ("src/atelier2", "tests"))


def test_a_claim_request_reads_back_as_the_value_that_wrote_it() -> None:
    request = _request()

    assert ClaimWorkItem.from_canonical_bytes(request.canonical_bytes()) == request


def test_the_same_run_and_item_always_name_the_same_claim() -> None:
    """The id is the retry's own handle: it is derived, never allocated."""

    assert work_item_claim_id(RUN, 1320) == CLAIM_ID
    assert work_item_claim_id(RUN, 1321) != CLAIM_ID
    assert work_item_claim_id(RunId("another-run"), 1320) != CLAIM_ID


@pytest.mark.parametrize(
    "make",
    (
        lambda: ClaimWorkItem(0, CLAIM_ID, BRANCH, ("src",)),
        lambda: ClaimWorkItem(1320, "a claim id with spaces", BRANCH, ("src",)),
        lambda: ClaimWorkItem(1320, CLAIM_ID, BRANCH, ()),
        lambda: ClaimWorkItem(1320, CLAIM_ID, BRANCH, ("tests", "src")),
        lambda: ClaimWorkItem(1320, CLAIM_ID, BRANCH, ("src", "src")),
    ),
    ids=(
        "no-item",
        "unsafe-claim-id",
        "empty-scope",
        "unsorted-scope",
        "duplicate-scope",
    ),
)
def test_a_claim_nothing_could_be_taken_under_is_refused(
    make: object,
) -> None:
    with pytest.raises(ValueError):
        make()  # type: ignore[operator]


def test_a_claim_receipt_reads_back_the_scope_and_lanes_the_ledger_answered() -> None:
    """The confirmed scope and the foreign lanes are separate answers, and stay so."""

    receipt = ClaimWorkItemReceipt(
        1320,
        CLAIM_ID,
        f"atelier2 run {RUN.value}",
        BRANCH,
        ("src/atelier2", "tests"),
        (ClaimedLanePath("other-claim", "atelier2 run other", ("tests",), 77),),
    )

    read = ClaimWorkItemReceipt.from_result_bytes(receipt.result_bytes())

    assert read == receipt
    assert read.claimed_scope == ("src/atelier2", "tests")
    assert read.touches[0].item == 77


def test_a_receipt_carrying_a_field_no_reader_knows_is_refused() -> None:
    receipt = ClaimWorkItemReceipt(1320, CLAIM_ID, "atelier2 run x", BRANCH, ("src",))
    payload = receipt.result_bytes().replace(b'"item"', b'"issue"')

    with pytest.raises(ValueError):
        ClaimWorkItemReceipt.from_result_bytes(payload)
