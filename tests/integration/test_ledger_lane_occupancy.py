"""The ledger question the queue sweep asks before the label admits an item.

The application sees only what `_LedgerLaneOccupancy` returns, so the
decisions this class makes for the sweep are pinned here against the claim
port's fake: which lane is the item's own, what an unreadable ledger means,
and how often one sweep asks the ledger at all.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

import pytest

from atelier2.adapters.dbos.runtime import _LedgerLaneOccupancy
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.ports.work_item_claims import (
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimTouch,
)
from tests.scenarios.work_item_claims import FakeWorkItemClaims

OWN_ITEM = TrackerItemReference("gh:1455")
OWN_PATH = "src/atelier2/application/advance_queue.py"
FOREIGN_PATH = "src/atelier2/application/execute_agent_attempt.py"


def _claim(item: int | None, path: str) -> ClaimTouch:
    return ClaimTouch(
        item, f"claim-{item}", f"atelier2 run of {item}", (PurePosixPath(path),)
    )


def test_the_items_own_lane_is_not_answered_as_a_foreign_one() -> None:
    own = _claim(1455, OWN_PATH)
    foreign = _claim(1446, FOREIGN_PATH)
    ledger = FakeWorkItemClaims(standing_claims_answer=(own, foreign))

    assert _LedgerLaneOccupancy(ledger).foreign_claims(OWN_ITEM) == (foreign,)


def test_an_item_in_another_trackers_grammar_has_nothing_held_against_it() -> None:
    ledger = FakeWorkItemClaims(standing_claims_answer=(_claim(1446, FOREIGN_PATH),))

    occupancy = _LedgerLaneOccupancy(ledger)

    assert occupancy.foreign_claims(TrackerItemReference("gl:group/project#7")) == ()


def test_a_ledger_that_does_not_answer_holds_nothing_back_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = FakeWorkItemClaims(
        standing_claims_answer=ClaimRefusal(
            ClaimRefusalReason.LEDGER_UNREADABLE, "the store could not be read"
        )
    )

    with caplog.at_level(logging.WARNING, logger="atelier2"):
        foreign = _LedgerLaneOccupancy(ledger).foreign_claims(OWN_ITEM)

    assert foreign == ()
    assert [
        getattr(record, "detail", None)
        for record in caplog.records
        if getattr(record, "event", None) == "queue_label_admission_ledger_unread"
    ] == ["the store could not be read"]


def test_one_sweep_asks_the_ledger_once_however_many_items_it_weighs() -> None:
    foreign = _claim(1446, FOREIGN_PATH)
    ledger = FakeWorkItemClaims(standing_claims_answer=(foreign,))
    occupancy = _LedgerLaneOccupancy(ledger)

    answers = (
        occupancy.foreign_claims(OWN_ITEM),
        occupancy.foreign_claims(TrackerItemReference("gh:1460")),
    )

    assert answers == ((foreign,), (foreign,))
    assert ledger.standing_claims_reads == 1
