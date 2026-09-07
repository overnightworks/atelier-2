"""Automatic admission by the project's automation label.

The label is the operator's signal in the tracker, so these scenarios drive
the real `admit_queue_items_by_label` against a projection that runs the real
`QueueItemSnapshot.confirm` CAS: what the rule may and may not admit is the
contract's own answer, and the assertions read the state the projection is
left in, never the calls it received.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Never

import pytest

from atelier2.application.advance_queue import (
    QueueAutomationLabelUnset,
    QueueAutomationSourceUnreadable,
    QueueLabelAdmissionsDecided,
    admit_queue_items_by_label,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QUEUE_PROJECTION_REVISION_OBSERVED,
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmission,
    QueueAdmissionAlreadyDecided,
    QueueAdmissionAuthorityRefused,
    QueueAdmissionOutcome,
    QueueAdmissionProposalRequired,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueDecisionAuthority,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemProposed,
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyDefaults,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalOutcome,
    QueueProposalSource,
    ReleaseQueueLaunch,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    OpenTrackerItemsObserved,
    TrackerSourceUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueItemsPage,
    QueueLaunchReleased,
    QueueLaunchRunOpen,
    QueueProjectPolicyAbsent,
    QueueProjectPolicyFound,
    ReadQueueProjectPolicyResult,
)
from tests.scenarios.issue_observation import FakeTrackerItemSource

PROJECT = ProjectId("studio")
LINEAGE = CatalogLineageId("b" * 64)
LABEL = "bereit"
OBSERVED_AT = RecordedAt("2026-09-04T09:00:00Z")
OPERATOR_RATIONALE = QueueAdmissionRationale("operator approved the proposal")
DEFAULT_PRIORITY = QueuePriorityRank(3)


def _reference(tracker: str) -> WorkItemReference:
    return WorkItemReference(PROJECT, TrackerItemReference(tracker))


def _observed(tracker: str) -> QueueItemSnapshot:
    return QueueItemSnapshot(
        _reference(tracker),
        QueueItemState.OBSERVED,
        QUEUE_PROJECTION_REVISION_OBSERVED,
        None,
    )


def _proposed(
    tracker: str,
    disposition: QueueAutomationDisposition = (
        QueueAutomationDisposition.AUTOMATION_AUTHORIZED
    ),
) -> QueueItemSnapshot:
    return QueueItemSnapshot(
        _reference(tracker),
        QueueItemState.PROPOSED,
        QueueProjectionRevision(1),
        None,
        QueueProposal(QueuePriorityRank(1), LINEAGE, (), disposition),
    )


def _admitted_by_the_operator(tracker: str) -> QueueItemSnapshot:
    return QueueItemSnapshot(
        _reference(tracker),
        QueueItemState.ADMITTED,
        QueueProjectionRevision(2),
        QueueAdmission(
            LINEAGE,
            OPERATOR_RATIONALE,
            QueueDecisionAuthority.OPERATOR,
            QueueProjectionRevision(1),
        ),
        QueueProposal(
            QueuePriorityRank(1),
            LINEAGE,
            (),
            QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
        ),
    )


@dataclass
class _QueueProjectionFake:
    """The projection an automatic admission reads, confirming through the contract.

    `confirm` is the real `QueueItemSnapshot.confirm` decision; this fake only
    keeps what a durable store would keep afterwards, so a scenario can assert
    the state each item was left in.
    """

    items: list[QueueItemSnapshot]
    policy: ReadQueueProjectPolicyResult = field(
        default_factory=lambda: QueueProjectPolicyFound(
            QueueProjectPolicyRevision(PROJECT, 1, 2, LABEL)
        )
    )
    released: list[ReleaseQueueLaunch] = field(default_factory=list)

    def current_policy(self, project: ProjectId) -> ReadQueueProjectPolicyResult:
        assert project == PROJECT
        return self.policy

    def list_items(self, after: QueueItemId | None, limit: int) -> QueueItemsPage:
        assert after is None, "this fixture serves exactly one page"
        return QueueItemsPage(tuple(self.items), None)

    def confirm(self, command: ConfirmQueueProposal) -> QueueAdmissionOutcome:
        index, snapshot = self._locate(command.item_reference.item_id)
        outcome = snapshot.confirm(command)
        if isinstance(outcome, QueueItemAdmitted):
            self.items[index] = QueueItemSnapshot(
                snapshot.item_reference,
                QueueItemState.ADMITTED,
                outcome.revision,
                outcome.admission,
                snapshot.proposal,
            )
        return outcome

    def state_of(self, tracker: str) -> QueueItemSnapshot:
        _, snapshot = self._locate(_reference(tracker).item_id)
        return snapshot

    def _locate(self, item_id: QueueItemId) -> tuple[int, QueueItemSnapshot]:
        for index, snapshot in enumerate(self.items):
            if snapshot.item_reference.item_id == item_id:
                return index, snapshot
        raise AssertionError(f"this scenario holds no item {item_id.value}")

    def plan(self, command: PlanQueueItem) -> QueueProposalOutcome:
        index, snapshot = self._locate(command.item_reference.item_id)
        outcome = snapshot.plan(command)
        if isinstance(outcome, QueueItemProposed):
            self.items[index] = QueueItemSnapshot(
                snapshot.item_reference,
                QueueItemState.PROPOSED,
                outcome.revision,
                None,
                outcome.proposal,
            )
        return outcome

    def put_policy(self, policy: object, expected_revision: object) -> Never:
        raise AssertionError("an automatic admission never publishes a policy")

    def reserve_launch(self, binding: object) -> Never:
        raise AssertionError("admission is not a start: no launch is reserved")

    def read_launch(self, binding: QueueLaunchBinding) -> QueueLaunchRunOpen:
        return QueueLaunchRunOpen()

    def release_launch(self, command: ReleaseQueueLaunch) -> QueueLaunchReleased:
        self.released.append(command)
        return QueueLaunchReleased()

    def reconcile_open_items(
        self, project: object, items: object, observed_at: object
    ) -> Never:
        raise AssertionError("an automatic admission never reconciles the open set")


@dataclass
class _QueueProjectionReadBeforeAnotherSweepWrote(_QueueProjectionFake):
    """A projection whose page was read before a concurrent sweep wrote to it.

    Two sweeps serialise on the durable write, so the second one plans and
    confirms against snapshots the first has already moved past. The page and
    the durable items are separate here for exactly that reason.
    """

    page: list[QueueItemSnapshot] = field(default_factory=list)

    def list_items(self, after: QueueItemId | None, limit: int) -> QueueItemsPage:
        assert after is None, "this fixture serves exactly one page"
        return QueueItemsPage(tuple(self.page), None)


def _policy_with_defaults(
    disposition: QueueAutomationDisposition | None = None,
) -> QueueProjectPolicyFound:
    """A policy naming the label and what an unproposed item is proposed under.

    An absent `disposition` is the operator saying nothing about it, which is
    not the same as saying HUMAN_REQUIRED here: the contract owns that answer.
    """

    defaults = (
        QueueProjectPolicyDefaults(LINEAGE, DEFAULT_PRIORITY)
        if disposition is None
        else QueueProjectPolicyDefaults(LINEAGE, DEFAULT_PRIORITY, disposition)
    )
    return QueueProjectPolicyFound(
        QueueProjectPolicyRevision(PROJECT, 1, 2, LABEL, defaults)
    )


def _proposed_from_the_defaults(tracker: str) -> QueueItemSnapshot:
    """The row a sweep leaves behind after writing the policy's own proposal."""

    return QueueItemSnapshot(
        _reference(tracker),
        QueueItemState.PROPOSED,
        QueueProjectionRevision(1),
        None,
        QueueProposal(
            DEFAULT_PRIORITY,
            LINEAGE,
            (),
            QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
            1,
            QueueProposalSource.POLICY_DEFAULT,
        ),
    )


def _tracker(*items: tuple[str, tuple[str, ...]]) -> FakeTrackerItemSource:
    return FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            tuple(
                ObservedOpenTrackerItem(
                    TrackerItemReference(reference), f"item {reference}", labels
                )
                for reference, labels in items
            ),
            OBSERVED_AT,
        )
    )


def _item_ids(*trackers: str) -> tuple[QueueItemId, ...]:
    return tuple(_reference(tracker).item_id for tracker in trackers)


@pytest.mark.proves("the-automation-label-admits-the-items-that-carry-it")
def test_a_labelled_proposal_is_admitted_by_the_rule_and_an_unlabelled_one_is_not() -> (
    None
):
    queue = _QueueProjectionFake([_proposed("gh:1"), _proposed("gh:2")])

    outcome = admit_queue_items_by_label(
        queue,
        project=PROJECT,
        tracker=_tracker(("gh:1", (LABEL,)), ("gh:2", ("frage",))),
    )

    assert outcome == QueueLabelAdmissionsDecided(_item_ids("gh:1"), ())
    admitted = queue.state_of("gh:1")
    assert admitted.state is QueueItemState.ADMITTED
    assert admitted.admission is not None
    assert admitted.admission.authority is QueueDecisionAuthority.AUTOMATION_RULE
    assert LABEL in admitted.admission.rationale.value
    assert admitted.admission.workflow_lineage_id == LINEAGE
    assert queue.state_of("gh:2").state is QueueItemState.PROPOSED


@pytest.mark.parametrize(
    "policy",
    [
        QueueProjectPolicyFound(QueueProjectPolicyRevision(PROJECT, 1, 2, None)),
        QueueProjectPolicyAbsent(),
    ],
    ids=["revision-without-a-label", "no-revision-at-all"],
)
@pytest.mark.proves("the-automation-label-admits-the-items-that-carry-it")
def test_without_a_label_in_the_policy_nothing_is_admitted_automatically(
    policy: ReadQueueProjectPolicyResult,
) -> None:
    queue = _QueueProjectionFake([_proposed("gh:1")], policy)

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert outcome == QueueAutomationLabelUnset()
    assert queue.state_of("gh:1").state is QueueItemState.PROPOSED


def test_an_item_already_admitted_by_the_operator_keeps_that_admission() -> None:
    queue = _QueueProjectionFake([_admitted_by_the_operator("gh:1")])

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert isinstance(outcome, QueueLabelAdmissionsDecided)
    assert outcome.admitted == ()
    (declined,) = outcome.declined
    assert isinstance(declined.outcome, QueueAdmissionAlreadyDecided)
    admission = queue.state_of("gh:1").admission
    assert admission is not None
    assert admission.authority is QueueDecisionAuthority.OPERATOR
    assert admission.rationale == OPERATOR_RATIONALE


def test_a_proposal_reserved_for_a_human_is_not_admitted_by_the_rule() -> None:
    queue = _QueueProjectionFake(
        [_proposed("gh:1", QueueAutomationDisposition.HUMAN_REQUIRED)]
    )

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert isinstance(outcome, QueueLabelAdmissionsDecided)
    assert outcome.admitted == ()
    (declined,) = outcome.declined
    assert declined.outcome == QueueAdmissionAuthorityRefused(
        QueueDecisionAuthority.AUTOMATION_RULE,
        QueueAutomationDisposition.HUMAN_REQUIRED,
    )
    assert queue.state_of("gh:1").state is QueueItemState.PROPOSED


def test_a_labelled_item_with_no_inspected_proposal_stays_observed() -> None:
    """The label says "go", never which workflow or priority to go with."""

    queue = _QueueProjectionFake([_observed("gh:1")])

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert isinstance(outcome, QueueLabelAdmissionsDecided)
    assert outcome.admitted == ()
    (declined,) = outcome.declined
    assert isinstance(declined.outcome, QueueAdmissionProposalRequired)
    assert queue.state_of("gh:1").state is QueueItemState.OBSERVED


@pytest.mark.proves("the-automation-label-admits-the-items-that-carry-it")
def test_a_labelled_item_is_proposed_from_the_policy_defaults_and_admitted() -> None:
    """With defaults published, the label alone is the operator's whole handgrip."""

    queue = _QueueProjectionFake(
        [_observed("gh:1")],
        _policy_with_defaults(QueueAutomationDisposition.AUTOMATION_AUTHORIZED),
    )

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert outcome == QueueLabelAdmissionsDecided(_item_ids("gh:1"), ())
    admitted = queue.state_of("gh:1")
    assert admitted.state is QueueItemState.ADMITTED
    assert admitted.proposal == _proposed_from_the_defaults("gh:1").proposal
    assert admitted.admission is not None
    assert admitted.admission.authority is QueueDecisionAuthority.AUTOMATION_RULE


@pytest.mark.proves("the-automation-label-admits-the-items-that-carry-it")
def test_a_second_sweep_admits_the_proposal_the_first_sweep_left_behind() -> None:
    """A sweep that loses the proposal write still admits the item it read.

    The write is serialised, so the losing sweep is answered with the proposal
    that is already current. Confirming the revision its own stale page named
    would leave the item merely proposed, waiting for a sweep that has nothing
    left to do.
    """

    queue = _QueueProjectionReadBeforeAnotherSweepWrote(
        [_proposed_from_the_defaults("gh:1")],
        _policy_with_defaults(QueueAutomationDisposition.AUTOMATION_AUTHORIZED),
        page=[_observed("gh:1")],
    )

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert outcome == QueueLabelAdmissionsDecided(_item_ids("gh:1"), ())
    admitted = queue.state_of("gh:1")
    assert admitted.state is QueueItemState.ADMITTED
    assert admitted.admission is not None
    assert admitted.admission.authority is QueueDecisionAuthority.AUTOMATION_RULE


def test_a_policy_default_the_operator_did_not_authorise_waits_for_a_human() -> None:
    """REQ-QUEUE-05: the queue writes the proposal, a person still releases it."""

    queue = _QueueProjectionFake([_observed("gh:1")], _policy_with_defaults())

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert isinstance(outcome, QueueLabelAdmissionsDecided)
    assert outcome.admitted == ()
    (declined,) = outcome.declined
    assert declined.outcome == QueueAdmissionAuthorityRefused(
        QueueDecisionAuthority.AUTOMATION_RULE,
        QueueAutomationDisposition.HUMAN_REQUIRED,
    )
    proposed = queue.state_of("gh:1")
    assert proposed.state is QueueItemState.PROPOSED
    assert proposed.proposal is not None
    assert (
        proposed.proposal.automation_disposition
        is QueueAutomationDisposition.HUMAN_REQUIRED
    )


def test_the_policy_defaults_never_replace_a_proposal_the_operator_wrote() -> None:
    """The defaults fill a missing decision; they do not revise an existing one."""

    queue = _QueueProjectionFake(
        [_proposed("gh:1")],
        _policy_with_defaults(QueueAutomationDisposition.AUTOMATION_AUTHORIZED),
    )

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert outcome == QueueLabelAdmissionsDecided(_item_ids("gh:1"), ())
    admitted = queue.state_of("gh:1")
    assert admitted.proposal is not None
    assert admitted.proposal.priority == QueuePriorityRank(1)
    assert admitted.proposal.source is QueueProposalSource.OPERATOR


def test_a_retired_row_is_not_admitted_even_while_the_tracker_lists_it() -> None:
    """A retired row has left the pullable set until an import brings it back."""

    queue = _QueueProjectionFake([replace(_proposed("gh:1"), retired_at=OBSERVED_AT)])

    outcome = admit_queue_items_by_label(
        queue, project=PROJECT, tracker=_tracker(("gh:1", (LABEL,)))
    )

    assert outcome == QueueLabelAdmissionsDecided((), ())
    assert queue.state_of("gh:1").state is QueueItemState.PROPOSED


def test_an_unreadable_tracker_admits_nothing_and_says_so() -> None:
    queue = _QueueProjectionFake([_proposed("gh:1")])
    tracker = FakeTrackerItemSource(
        open_items_answer=TrackerSourceUnavailable("GitHub could not be reached")
    )

    outcome = admit_queue_items_by_label(queue, project=PROJECT, tracker=tracker)

    assert outcome == QueueAutomationSourceUnreadable("GitHub could not be reached")
    assert queue.state_of("gh:1").state is QueueItemState.PROPOSED
