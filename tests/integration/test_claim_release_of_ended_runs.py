"""What the sweep does with the claim a run left behind when it ended.

The store, the claim ledger and the head branch each answer one part, and
this pins the decision made from those three answers: which claim is given
back, as what, and which one is left exactly where it stands.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.claim_release import (
    NO_PULL_REQUEST_RELEASE_REASON,
    UNLANDED_RELEASE_REASON,
    release_claims_of_ended_runs,
)
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    initialize_schema,
    run_configuration_revisions,
    runs,
    workflow_revisions,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    ClaimWorkItemReceipt,
    HeadBranch,
    work_item_claim_id,
)
from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectIntentState,
    UnknownOutcomeReason,
)
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, RunId, RunState
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.effects import (
    HeadBranchPullRequestsUnreadable,
    PullRequestOpenOnHeadBranch,
)
from atelier2.ports.work_item_claims import (
    Abandoned,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimReleaseOutcome,
    ClaimTouch,
    Merged,
)
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.work_item_claims import FakeWorkItemClaims

ITEM = 1481
PULL_REQUEST = 4321
BRANCH = HeadBranch("atelier2/work-item/gh-1481")
SCOPE = PurePosixPath("src/atelier2/adapters/dbos/claim_release.py")
LEDGER_REFUSAL = "pull request #4321 is not merged"


@dataclass(frozen=True, slots=True)
class _EndedRun:
    """One seeded run, named the way the ledger and the store name it."""

    run_id: RunId
    item: int
    claim_id: str
    branch: HeadBranch


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _seed_run(
    engine: Engine,
    *,
    run_id: str = "run-of-the-ended-lane",
    state: RunState = RunState.COMPLETED,
    pull_requests: tuple[int, ...] = (),
    claim_receipts: int = 1,
) -> _EndedRun:
    """A run in `state` that receipted its claim and the pull requests it opened.

    `claim_receipts` is how many of the run's nodes recorded the one claim it
    holds: several do when several of them publish.
    """

    revision_hash = _digest(f"revision of {run_id}".encode())
    configuration_hash = _digest(f"configuration of {run_id}".encode())
    identity = RunId(run_id)
    claim_id = work_item_claim_id(identity, ITEM)
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision_hash, document=b"format_version: 3\n"
            )
        )
        connection.execute(
            run_configuration_revisions.insert().values(
                revision_hash=configuration_hash, preimage=b"seeded run configuration"
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=run_id,
                bootstrap_workflow_id=f"bootstrap-{run_id}",
                revision_hash=revision_hash,
                workflow_format_version=int(WorkflowFormatVersion.V3),
                run_configuration_revision_hash=configuration_hash,
                current_node_id="builder",
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state=state.value,
                state_version=1,
                last_event_sequence=1,
                terminal_hash=(
                    _digest(f"terminal of {run_id}".encode())
                    if state in {RunState.COMPLETED, RunState.FAILED}
                    else None
                ),
            )
        )
        for ordinal in range(claim_receipts):
            _seed_effect(
                connection,
                run_id=run_id,
                revision_hash=revision_hash,
                operation=AdapterOperationName.CLAIM_WORK_ITEM,
                logical_key=f"{run_id}-claim-{ordinal}",
                effect_id=claim_id,
                result=ClaimWorkItemReceipt(
                    ITEM,
                    claim_id,
                    f"atelier2 run {run_id}",
                    BRANCH,
                    (SCOPE.as_posix(),),
                ).result_bytes(),
            )
        for pull_request in pull_requests:
            _seed_effect(
                connection,
                run_id=run_id,
                revision_hash=revision_hash,
                operation=AdapterOperationName.OPEN_PR,
                logical_key=f"{run_id}-open-pr-{pull_request}",
                effect_id=str(pull_request),
                result=f'{{"branch":"{BRANCH.value}","pr_number":{pull_request}}}'.encode(),
            )
    return _EndedRun(identity, ITEM, claim_id, BRANCH)


def _seed_effect(
    connection: sa.Connection,
    *,
    run_id: str,
    revision_hash: str,
    operation: AdapterOperationName,
    logical_key: str,
    effect_id: str,
    result: bytes,
) -> None:
    request = f"request of {logical_key}".encode()
    binding = {
        "logical_key": logical_key,
        "run_id": run_id,
        "canonical_request": request,
        "request_hash": _digest(request),
        "workflow_revision_hash": revision_hash,
        "adapter_revision": "seeded-adapter/1.0.0",
        "destination_identity": "seeded-destination",
        "adapter_operational_identity": "seeded-identity",
        "operation_name": operation.value,
    }
    connection.execute(
        effect_intents.insert().values(
            **binding, state=EffectIntentState.CONFIRMED.value, state_version=1
        )
    )
    connection.execute(
        effect_receipts.insert().values(
            **binding,
            effect_id=effect_id,
            result=result,
            result_hash=_digest(result),
            confirmation_source=ConfirmationSource.ADAPTER_EXECUTION.value,
        )
    )


def _unknown_outcome() -> UnknownOutcomeReason:
    return UnknownOutcomeReason(503, 12, "github did not answer the listing")


def _standing(ended: _EndedRun) -> ClaimTouch:
    return ClaimTouch(
        ended.item, ended.claim_id, f"atelier2 run {ended.run_id.value}", (SCOPE,)
    )


def _released_outcomes(ledger: FakeWorkItemClaims) -> list[ClaimReleaseOutcome]:
    return [outcome for *_, outcome in ledger.release_requests]


def test_a_landed_pull_request_gives_the_claim_back(engine: Engine) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST,))
    ledger = FakeWorkItemClaims(standing_claims_answer=(_standing(ended),))
    reviews = FakeHeadBranchPullRequests()

    release_claims_of_ended_runs(engine, ledger, reviews)

    assert ledger.release_requests == [
        (ended.item, ended.run_id, ended.claim_id, Merged(PULL_REQUEST))
    ]
    assert reviews.asked == [BRANCH]


def test_a_pull_request_still_open_keeps_the_claim_standing(engine: Engine) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST,))
    ledger = FakeWorkItemClaims(standing_claims_answer=(_standing(ended),))

    release_claims_of_ended_runs(
        engine,
        ledger,
        FakeHeadBranchPullRequests(PullRequestOpenOnHeadBranch(PULL_REQUEST)),
    )

    assert ledger.release_requests == []


def test_a_pull_request_that_ended_without_a_landing_is_abandoned(
    engine: Engine,
) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST,))
    ledger = FakeWorkItemClaims(
        standing_claims_answer=(_standing(ended),),
        release_answer=ClaimRefusal(ClaimRefusalReason.UNKNOWN, LEDGER_REFUSAL),
    )

    release_claims_of_ended_runs(engine, ledger, FakeHeadBranchPullRequests())

    assert _released_outcomes(ledger) == [
        Merged(PULL_REQUEST),
        Abandoned(f"{UNLANDED_RELEASE_REASON}: {LEDGER_REFUSAL}"),
    ]


def test_a_run_that_published_no_pull_request_is_abandoned(engine: Engine) -> None:
    ended = _seed_run(engine)
    ledger = FakeWorkItemClaims(standing_claims_answer=(_standing(ended),))
    reviews = FakeHeadBranchPullRequests()

    release_claims_of_ended_runs(engine, ledger, reviews)

    assert _released_outcomes(ledger) == [Abandoned(NO_PULL_REQUEST_RELEASE_REASON)]
    assert reviews.asked == []


def test_an_unreadable_head_branch_leaves_the_claim_and_asks_again(
    engine: Engine,
) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST,))
    ledger = FakeWorkItemClaims(standing_claims_answer=(_standing(ended),))
    reviews = FakeHeadBranchPullRequests(
        HeadBranchPullRequestsUnreadable(_unknown_outcome())
    )

    release_claims_of_ended_runs(engine, ledger, reviews)
    release_claims_of_ended_runs(engine, ledger, reviews)

    assert ledger.release_requests == []
    assert reviews.asked == [BRANCH, BRANCH]


def test_a_claim_of_the_same_item_held_by_a_person_is_not_touched(
    engine: Engine,
) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST,))
    by_hand = ClaimTouch(ended.item, "1dee01559f53", "felix", (SCOPE,))
    ledger = FakeWorkItemClaims(standing_claims_answer=(by_hand,))

    release_claims_of_ended_runs(engine, ledger, FakeHeadBranchPullRequests())

    assert ledger.release_requests == []


def test_only_the_runs_own_claim_id_is_released_beside_a_persons_claim(
    engine: Engine,
) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST,))
    by_hand = ClaimTouch(ended.item, "1dee01559f53", "felix", (SCOPE,))
    ledger = FakeWorkItemClaims(standing_claims_answer=(by_hand, _standing(ended)))

    release_claims_of_ended_runs(engine, ledger, FakeHeadBranchPullRequests())

    assert ledger.release_requests == [
        (ended.item, ended.run_id, ended.claim_id, Merged(PULL_REQUEST))
    ]


def test_a_pass_over_a_claim_already_given_back_says_and_does_nothing(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_run(engine, pull_requests=(PULL_REQUEST,))
    ledger = FakeWorkItemClaims(standing_claims_answer=())

    with caplog.at_level(logging.WARNING, logger="atelier2"):
        release_claims_of_ended_runs(engine, ledger, FakeHeadBranchPullRequests())

    assert ledger.release_requests == []
    assert caplog.records == []


def test_a_run_that_has_not_ended_keeps_its_claim(engine: Engine) -> None:
    ended = _seed_run(engine, state=RunState.STARTED, pull_requests=(PULL_REQUEST,))
    ledger = FakeWorkItemClaims(standing_claims_answer=(_standing(ended),))

    release_claims_of_ended_runs(engine, ledger, FakeHeadBranchPullRequests())

    assert ledger.release_requests == []


def test_a_claim_receipted_by_several_nodes_is_given_back_once(
    engine: Engine,
) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST,), claim_receipts=2)
    ledger = FakeWorkItemClaims(standing_claims_answer=(_standing(ended),))

    release_claims_of_ended_runs(engine, ledger, FakeHeadBranchPullRequests())

    assert _released_outcomes(ledger) == [Merged(PULL_REQUEST)]


def test_more_than_one_published_pull_request_leaves_the_claim_standing(
    engine: Engine,
) -> None:
    ended = _seed_run(engine, pull_requests=(PULL_REQUEST, PULL_REQUEST + 1))
    ledger = FakeWorkItemClaims(standing_claims_answer=(_standing(ended),))
    reviews = FakeHeadBranchPullRequests()

    release_claims_of_ended_runs(engine, ledger, reviews)

    assert ledger.release_requests == []
    assert reviews.asked == []


def test_an_unreadable_ledger_releases_nothing(engine: Engine) -> None:
    _seed_run(engine, pull_requests=(PULL_REQUEST,))
    ledger = FakeWorkItemClaims(
        standing_claims_answer=ClaimRefusal(
            ClaimRefusalReason.LEDGER_UNREADABLE, "the store could not be read"
        )
    )
    reviews = FakeHeadBranchPullRequests()

    release_claims_of_ended_runs(engine, ledger, reviews)

    assert ledger.release_requests == []
    assert reviews.asked == []
