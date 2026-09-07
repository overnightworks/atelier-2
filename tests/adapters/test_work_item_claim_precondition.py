"""What the builder node's claim precondition does with the ledger's answers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
import sqlalchemy as sa

from atelier2.adapters.agent_claim_cli import AgentClaimCli
from atelier2.adapters.dbos.node_binding_codec import decode_node_binding
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    effect_receipts,
    run_events,
    runs,
)
from atelier2.adapters.dbos.work_item_claims import (
    ConfirmedWorkItemClaim,
    WorkItemClaimHeld,
    WorkItemClaimLedger,
    WorkItemClaimRefused,
    hold_prepared_claim,
    hold_work_item_claim,
)
from atelier2.adapters.dbos.workflow import _node_binding
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
from atelier2.contracts.executions import AgentExecutionRefusal, RunEventKind
from atelier2.contracts.node_bindings import AgentNodeBindingV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.ports.work_item_claims import (
    ClaimAbsent,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimTouch,
)
from tests.acceptance.test_v3_push_before_open_pr import (
    _SCOPED_ITEM,
    PROJECT,
    RUN,
    _public_runtime,
    _repositories,
    _start_public_run,
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
def test_every_ledger_refusal_ends_the_node_under_its_own_word_and_sentence(
    reason: ClaimRefusalReason, refusal: AgentExecutionRefusal
) -> None:
    sentence = "claim branch 'x' does not match checkout branch 'main'"
    claims = FakeWorkItemClaims(claim_answer=ClaimRefusal(reason, sentence))

    assert _held(claims) == WorkItemClaimRefused(refusal, sentence)


def test_an_unreadable_ledger_refuses_before_any_claim_is_attempted() -> None:
    sentence = "1 claim(s) in the ledger are unreadable to this tool"
    claims = FakeWorkItemClaims(
        read_back_answer=ClaimRefusal(ClaimRefusalReason.LEDGER_UNREADABLE, sentence)
    )

    outcome = _held(claims)

    assert claims.claim_requests == []
    assert outcome == WorkItemClaimRefused(
        AgentExecutionRefusal.WORK_ITEM_CLAIM_LEDGER_UNREADABLE, sentence
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


@dataclass
class _RecordedSteps:
    """The step record a recovered workflow answers from, in place of DBOS's own.

    A step already recorded answers from its record and does not run again; one
    not yet recorded runs against the real store and is recorded. Two passes over
    one record are what a node's first drive and its recovery are.
    """

    run: Callable[..., object]
    recorded: dict[str, object] = field(default_factory=dict)

    def run_tx_step(
        self, options: Mapping[str, object], step: Callable[[], object]
    ) -> object:
        name = str(options["name"])
        if name not in self.recorded:
            self.recorded[name] = self.run(options, step)
        return self.recorded[name]


@dataclass(frozen=True)
class _StartedBuilderNode:
    """The shipped line's builder node, started on a scoped item and not yet driven."""

    runtime: DbosRuntime
    revision_hash: WorkflowRevisionHash
    binding: AgentNodeBindingV2

    def hold(self, ledger: WorkItemClaimLedger) -> str | None:
        return hold_work_item_claim(
            self.runtime.datasource,
            ledger,
            PROJECT,
            self.binding,
            RUN,
            self.revision_hash,
            "implement",
        )

    def standing(self) -> tuple[str, int, int, int]:
        """The run's state, and how many receipts, refusals and attempts stand."""

        with self.runtime.engine.connect() as connection:
            state = connection.execute(
                sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
            ).scalar_one()
            receipts = connection.execute(
                sa.select(sa.func.count()).select_from(effect_receipts)
            ).scalar_one()
            refusals = connection.execute(
                sa.select(sa.func.count())
                .select_from(run_events)
                .where(run_events.c.event_kind == RunEventKind.AGENT_FAILED.value)
            ).scalar_one()
            attempts = connection.execute(
                sa.select(sa.func.count()).select_from(agent_attempts)
            ).scalar_one()
        return (str(state), int(receipts), int(refusals), int(attempts))


@pytest.fixture
def started_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_StartedBuilderNode]:
    """One builder node whose durable steps are recorded and replayed in-process."""

    project, remote, _base = _repositories(tmp_path)
    runtime, _github = _public_runtime(tmp_path, project, remote)
    try:
        yield _started_node(runtime, monkeypatch)
    finally:
        runtime.close()


def _started_node(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> _StartedBuilderNode:
    assert _start_public_run(runtime, _SCOPED_ITEM).status_code == 201
    with runtime.engine.connect() as connection:
        revision_hash = WorkflowRevisionHash(
            connection.execute(
                sa.select(runs.c.revision_hash).where(runs.c.run_id == RUN.value)
            ).scalar_one()
        )
    binding = decode_node_binding(
        dict(
            _node_binding(
                runtime.datasource,
                RUN,
                revision_hash,
                "implement",
                runtime.declared_project,
            )
        )
    )
    assert isinstance(binding, AgentNodeBindingV2)
    monkeypatch.setattr(
        runtime.datasource,
        "run_tx_step",
        _RecordedSteps(runtime.datasource.run_tx_step).run_tx_step,
    )
    return _StartedBuilderNode(runtime, revision_hash, binding)


def _refusing_everything() -> FakeWorkItemClaims:
    """A ledger that became unreadable and refuses every claim: the worst later answer."""

    return FakeWorkItemClaims(
        claim_answer=ClaimRefusal(ClaimRefusalReason.PRIORITY),
        read_back_answer=ClaimRefusal(ClaimRefusalReason.LEDGER_UNREADABLE),
    )


def test_a_recovery_replays_the_held_claim_without_asking_the_ledger_again(
    started_node: _StartedBuilderNode, tmp_path: Path
) -> None:
    """The claim decision is taken once: a replay holds what the first drive held.

    The ledger now refuses everything, as one that became unreadable or gained
    a foreign lane on these paths would -- and the replay never asks it, so
    the run stays STARTED with its one receipt while the attempt is in flight.
    """

    executable = fake_agent_claim_executable(tmp_path)
    first_drive = WorkItemClaimLedger(
        AgentClaimCli(executable, tmp_path), LEDGER_BINDING
    )
    assert started_node.hold(first_drive) is None
    assert len(claimed_ledger(executable)) == 1
    held = started_node.standing()

    replay = _refusing_everything()
    assert started_node.hold(WorkItemClaimLedger(replay, LEDGER_BINDING)) is None

    assert (replay.read_back_requests, replay.claim_requests) == ([], [])
    assert held == (RunState.STARTED.value, 1, 0, 0)
    assert started_node.standing() == held


def test_a_recovery_after_a_refusal_refuses_once_and_builds_on_no_later_grant(
    started_node: _StartedBuilderNode, tmp_path: Path
) -> None:
    """A node that ended on a refusal ends there again, whatever the ledger says now.

    The replay's ledger would grant the claim; it is never asked, no claim is
    posted, no receipt is written, the one refusal stands, and the answer is the
    same terminal word -- so no attempt is started on a run already FAILED.
    """

    first_drive = WorkItemClaimLedger(
        FakeWorkItemClaims(claim_answer=ClaimRefusal(ClaimRefusalReason.PRIORITY)),
        LEDGER_BINDING,
    )
    assert started_node.hold(first_drive) == RunState.FAILED.value
    refused = started_node.standing()

    executable = fake_agent_claim_executable(tmp_path)
    replay = WorkItemClaimLedger(AgentClaimCli(executable, tmp_path), LEDGER_BINDING)
    assert started_node.hold(replay) == RunState.FAILED.value

    assert claimed_ledger(executable) == ()
    assert refused == (RunState.FAILED.value, 0, 1, 0)
    assert started_node.standing() == refused
