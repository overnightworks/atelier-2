"""advance_queue's own concern: order, and what a started run carries.

`queue_start_order_key` (contracts/queue_projection.py) is the one owner of
this ordering rule; `advance_queue` starts admitted items in that order, and
`GET /queue-items` (the DBOS store, tested at the integration layer) lists
every item in the same order. This file pins the rule and the wiring that
turns a bound document's `graph_inputs` into the order a start carries --
without a database, and through the real `start_published_run` a fake
`DurablePublishedRunStarter` answers, never by replacing that production
function itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Never, cast

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.advance_queue import (
    QueueItemBlocked,
    QueueItemRestarting,
    QueueItemRestartsExhausted,
    QueueItemRestartWithheld,
    QueueRunAlreadyActive,
    QueueRunStarted,
    advance_queue,
)
from atelier2.contracts.catalog_v3 import CatalogLineageDisplayName, CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.orders import ObservedWorkItemOrderValue, WorkItemOrderValue
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_LAUNCH_RESTARTS,
    QueueAdmission,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueRestartRefusal,
    ReleaseQueueLaunch,
    TrackerItemReference,
    WorkItemReference,
    queue_start_order_key,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableWorkItemOrderUnread,
    StartPublishedRunRequest,
    StartPublishedRunRequestV3,
)
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    OpenTrackerItemsObserved,
    TrackerSourceUnavailable,
    WorkItemRevisionObserved,
)
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogRevisionPosition,
    PublishedRevisionFound,
    ResolveCatalogNameResult,
)
from atelier2.ports.queue_projection import (
    QueueItemsPage,
    QueueLaunchReleased,
    QueueLaunchReserved,
    QueueLaunchRunEnded,
    QueueLaunchRunOpen,
    QueueProjectPolicyAbsent,
    QueueProjectPolicyFound,
    ReadQueueLaunchResult,
    ReadQueueProjectPolicyResult,
)
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    V3_WAIT_LINE_DOCUMENT,
    graph_input_wait_line,
)

PROJECT = ProjectId("studio")
LINEAGE = CatalogLineageId("b" * 64)
REVISION_HASH = PublishedRevisionHash("c" * 64)
OTHER_LINEAGE = CatalogLineageId("d" * 64)
OTHER_REVISION_HASH = PublishedRevisionHash("e" * 64)
RATIONALE = QueueAdmissionRationale("operator approved the inspected proposal")
LABEL = "bereit"
POLICY = QueueProjectPolicyRevision(PROJECT, 1, 5, LABEL)


def _tracker_listing(*open_items: tuple[str, bool]) -> FakeTrackerItemSource:
    """The tracker's open set: each reference, with or without the automation label."""

    return FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            tuple(
                ObservedOpenTrackerItem(
                    TrackerItemReference(reference),
                    f"open item {reference}",
                    (LABEL,) if labelled else (),
                )
                for reference, labelled in open_items
            )
        )
    )


WORK_ITEM_WORKFLOW_DOCUMENT = graph_input_wait_line(
    WORK_ITEM_ORDER_SCHEMA_REVISION.value
)
UNFILLABLE_WORKFLOW_DOCUMENT = graph_input_wait_line(
    ANY_JSON_SCHEMA.revision_hash.value
)


def _admitted(
    tracker: str, rank: int, lineage: CatalogLineageId = LINEAGE
) -> QueueItemSnapshot:
    reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
    proposal = QueueProposal(
        QueuePriorityRank(rank),
        lineage,
        (),
        QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
    )
    admission = QueueAdmission(
        lineage, RATIONALE, QueueDecisionAuthority.OPERATOR, QueueProjectionRevision(1)
    )
    return QueueItemSnapshot(
        reference,
        QueueItemState.ADMITTED,
        QueueProjectionRevision(2),
        admission,
        proposal,
    )


def _bound(tracker: str, rank: int, run_id: RunId) -> QueueItemSnapshot:
    """An admitted item holding the one launch its admitted proposal reserved."""

    item = _admitted(tracker, rank)
    admission = item.admission
    assert admission is not None
    assert admission.proposal_revision is not None
    return replace(
        item,
        launch_binding=QueueLaunchBinding(
            item.item_reference.item_id,
            admission.proposal_revision,
            run_id,
            WorkflowRevisionHash(REVISION_HASH.value),
        ),
    )


def _readmitted(item: QueueItemSnapshot) -> QueueItemSnapshot:
    """What the store answers about a released item: unbound, one revision on."""

    admission = item.admission
    assert admission is not None
    assert admission.proposal_revision is not None
    return replace(
        item,
        revision=QueueProjectionRevision(item.revision.value + 1),
        admission=replace(
            admission,
            proposal_revision=QueueProjectionRevision(
                admission.proposal_revision.value + 1
            ),
        ),
        launch_binding=None,
    )


def _legacy_admitted(tracker: str) -> QueueItemSnapshot:
    """An item admitted before proposals existed: no proposal, no rank."""

    reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
    admission = QueueAdmission(LINEAGE, RATIONALE)
    return QueueItemSnapshot(
        reference, QueueItemState.ADMITTED, QueueProjectionRevision(1), admission
    )


@dataclass
class _QueueRecording:
    """The queue projection reduced to what `advance_queue` may do with it."""

    page: QueueItemsPage
    endings: dict[RunId, ReadQueueLaunchResult] = field(default_factory=dict)
    policy: QueueProjectPolicyRevision | None = POLICY
    reserved: list[QueueLaunchBinding] = field(default_factory=list)
    released: list[ReleaseQueueLaunch] = field(default_factory=list)

    def list_items(self, after: QueueItemId | None, limit: int) -> QueueItemsPage:
        assert after is None, "this fixture serves exactly one page"
        return self.page

    def reserve_launch(self, binding: QueueLaunchBinding) -> QueueLaunchReserved:
        self.reserved.append(binding)
        return QueueLaunchReserved(binding)

    def read_launch(self, binding: QueueLaunchBinding) -> ReadQueueLaunchResult:
        return self.endings.get(binding.run_id, QueueLaunchRunOpen())

    def release_launch(self, command: ReleaseQueueLaunch) -> QueueLaunchReleased:
        """Answer the release, and serve what the store would answer afterwards."""

        self.released.append(command)
        released_item = command.binding.item_id
        self.page = QueueItemsPage(
            tuple(
                _readmitted(item)
                if item.item_reference.item_id == released_item
                else item
                for item in self.page.items
            ),
            self.page.next_after,
        )
        return QueueLaunchReleased()

    def plan(self, command: object) -> Never:
        raise AssertionError("advance_queue never plans a proposal")

    def confirm(self, command: object) -> Never:
        raise AssertionError("advance_queue never confirms an admission")

    def put_policy(self, policy: object, expected_revision: object) -> Never:
        raise AssertionError("advance_queue never publishes a policy")

    def current_policy(self, project: object) -> ReadQueueProjectPolicyResult:
        assert project == PROJECT
        if self.policy is None:
            return QueueProjectPolicyAbsent()
        return QueueProjectPolicyFound(self.policy)

    def reconcile_open_items(
        self, project: object, items: object, observed_at: object
    ) -> Never:
        raise AssertionError("advance_queue never reconciles the open set")


@dataclass
class _CatalogResolverStub:
    """Every lineage resolves by name to its configured head; `resolve` reads
    the document that head is bound to.

    A scenario that never touches a document (`workflow_document_parser` left
    unwired) never has to populate `documents`: `resolve` is only ever called
    once a bound revision hash needs its `graph_inputs` read.
    """

    heads: dict[CatalogLineageId, PublishedRevisionHash]
    documents: dict[PublishedRevisionHash, bytes] = field(default_factory=dict)

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> PublishedRevisionFound:
        assert kind is RevisionKind.WORKFLOW
        document = self.documents.get(revision_hash)
        if document is None:
            raise AssertionError(
                f"this scenario bound no document to {revision_hash!r}"
            )
        return PublishedRevisionFound(
            PublishedRevision(RevisionKind.WORKFLOW, document)
        )

    def resolve_reference(
        self, kind: object, lineage_id: object, revision_hash: object
    ) -> Never:
        raise AssertionError("advance_queue only resolves a lineage by name")

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: object,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        assert kind is RevisionKind.WORKFLOW
        assert position == "head"
        lineage_id = cast(CatalogLineageId, lineage_id_or_name)
        return CatalogNameFound(
            lineage_id,
            self.heads[lineage_id],
            1,
            CatalogLineageDisplayName("fixture"),
            False,
        )


ScriptedAnswer = (
    DurablePublishedRunResult
    | Callable[[AnyStartPublishedRunRequest], DurablePublishedRunResult]
)


@dataclass
class _ScriptedStarter:
    """A store that answers each ask in turn, remembering what it was handed.

    A scripted answer may be a callable of the request itself, so a "created"
    answer can echo the request's own run id and revision hash back rather
    than a test re-deriving `advance_queue`'s own `RunId` derivation to
    predict them.
    """

    answers: list[ScriptedAnswer]
    asks: list[AnyStartPublishedRunRequest] = field(default_factory=list)

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        self.asks.append(request)
        answer = self.answers[len(self.asks) - 1]
        return answer(request) if callable(answer) else answer


def _created(request: AnyStartPublishedRunRequest) -> DurablePublishedRunResult:
    return DurableRunCreated(
        Run(request.run_id, request.revision_hash, RunState.STARTED, "final", 0, 0)
    )


def test_queue_start_order_key_ranks_proposals_first_then_by_rank_then_by_item_id() -> (
    None
):
    low_rank = _admitted("gh:100", rank=1)
    tie_first = _admitted("gh:150", rank=2)
    tie_second = _admitted("gh:200", rank=2)
    unranked = _legacy_admitted("gh:900")
    assert (
        tie_first.item_reference.item_id.value < tie_second.item_reference.item_id.value
    )

    ordered = sorted(
        [unranked, tie_second, low_rank, tie_first], key=queue_start_order_key
    )

    assert ordered == [low_rank, tie_first, tie_second, unranked]


def test_advance_queue_starts_admitted_items_in_the_shared_order_key() -> None:
    low_rank = _admitted("gh:100", rank=1)
    tie_first = _admitted("gh:150", rank=2)
    tie_second = _admitted("gh:200", rank=2)
    unranked = _legacy_admitted("gh:900")
    queue = _QueueRecording(
        QueueItemsPage((unranked, tie_second, low_rank, tie_first), None)
    )
    catalog = _CatalogResolverStub({LINEAGE: REVISION_HASH})
    starter = _ScriptedStarter([_created, _created, _created])

    outcomes = advance_queue(queue, catalog, starter, workflow_document_parser=None)

    assert [outcome.item_id for outcome in outcomes] == [
        low_rank.item_reference.item_id,
        tie_first.item_reference.item_id,
        tie_second.item_reference.item_id,
        unranked.item_reference.item_id,
    ]
    assert all(isinstance(outcome, QueueRunStarted) for outcome in outcomes[:3])
    (legacy_outcome,) = outcomes[3:]
    assert isinstance(legacy_outcome, QueueItemBlocked)
    assert legacy_outcome.blockers == (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)


def test_a_document_with_no_graph_inputs_starts_exactly_as_before() -> None:
    """No `graph_inputs` means no order to fill: `bindings` stays `None`."""

    item = _admitted("gh:301", rank=1)
    queue = _QueueRecording(QueueItemsPage((item,), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH}, {REVISION_HASH: V3_WAIT_LINE_DOCUMENT}
    )
    starter = _ScriptedStarter([_created])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
    )

    assert isinstance(outcome, QueueRunStarted)
    (asked,) = starter.asks
    assert isinstance(asked, StartPublishedRunRequest)


@pytest.mark.proves("a-manually-approved-queue-item-starts-once")
def test_a_bound_graph_input_workflow_starts_carrying_the_items_tracker_reference() -> (
    None
):
    """The order names the item; `bindings` becomes `()`, never `None`.

    This also pins the `DurableRunIdentityConflict` argument the plan review
    named: the derived `RunId` is fresh here (this item was never bound
    before), and the old code path could never have written a row under it --
    a graph-input workflow always refused before any insert. A first start of
    a fresh item must therefore create, never conflict -- one exact run, as
    #79's ruled line 6 requires whether or not the bound workflow needs order
    material.
    """

    reference = TrackerItemReference("gh:501")
    revision = ObservedWorkItemRevision(
        reference,
        WorkItemKind.ISSUE,
        b"what the item said",
        WorkItemChangeMarker('W/"1"'),
        RecordedAt("2026-09-04T09:00:00Z"),
    )
    item = _admitted(reference.value, rank=1)
    queue = _QueueRecording(QueueItemsPage((item,), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH}, {REVISION_HASH: WORK_ITEM_WORKFLOW_DOCUMENT}
    )
    tracker = FakeTrackerItemSource(snapshot_answer=WorkItemRevisionObserved(revision))
    starter = _ScriptedStarter([DurableWorkItemOrderUnread(), _created])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
        tracker=tracker,
    )

    assert isinstance(outcome, QueueRunStarted)
    first_ask, second_ask = starter.asks
    assert isinstance(first_ask, StartPublishedRunRequestV3)
    assert first_ask.agent_bindings.bindings == ()
    assert first_ask.orders[0].value == WorkItemOrderValue(reference)
    assert isinstance(second_ask, StartPublishedRunRequestV3)
    assert second_ask.orders[0].value == ObservedWorkItemOrderValue(revision)


def test_a_document_declaring_more_than_the_sweep_can_fill_is_blocked_not_guessed_at() -> (
    None
):
    item = _admitted("gh:601", rank=1)
    queue = _QueueRecording(QueueItemsPage((item,), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH}, {REVISION_HASH: UNFILLABLE_WORKFLOW_DOCUMENT}
    )
    starter = _ScriptedStarter([])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
    )

    assert isinstance(outcome, QueueItemBlocked)
    assert outcome.blockers == (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,)
    assert starter.asks == []


@pytest.mark.proves("a-refused-queue-start-stays-at-its-item-while-the-sweep-continues")
def test_a_disconnected_tracker_blocks_only_the_item_that_needs_it() -> None:
    needs_tracker = _admitted("gh:701", rank=1)
    plain = _admitted("gh:702", rank=2, lineage=OTHER_LINEAGE)
    queue = _QueueRecording(QueueItemsPage((needs_tracker, plain), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH, OTHER_LINEAGE: OTHER_REVISION_HASH},
        {
            REVISION_HASH: WORK_ITEM_WORKFLOW_DOCUMENT,
            OTHER_REVISION_HASH: V3_WAIT_LINE_DOCUMENT,
        },
    )
    starter = _ScriptedStarter([DurableWorkItemOrderUnread(), _created])

    blocked_outcome, started_outcome = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
    )

    assert isinstance(blocked_outcome, QueueItemBlocked)
    assert blocked_outcome.blockers == (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,)
    assert isinstance(started_outcome, QueueRunStarted)


def test_a_foreign_project_item_is_skipped_while_a_served_item_still_starts() -> None:
    """A foreign `project_id` reaches an admitted row through `PUT
    /queue-proposals`, or the served project changes with old rows left behind
    (review finding 1 on `#1145`): neither is this instance's item, so the
    sweep leaves it exactly as it found it -- no launch binding, no run, no
    blocker invented -- and keeps going with the next admitted item.
    """

    other_project = ProjectId("elsewhere")
    foreign_reference = WorkItemReference(other_project, TrackerItemReference("gh:801"))
    foreign_item = QueueItemSnapshot(
        foreign_reference,
        QueueItemState.ADMITTED,
        QueueProjectionRevision(2),
        QueueAdmission(
            LINEAGE,
            RATIONALE,
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
    served_item = _admitted("gh:802", rank=2)
    queue = _QueueRecording(QueueItemsPage((foreign_item, served_item), None))
    catalog = _CatalogResolverStub({LINEAGE: REVISION_HASH})
    starter = _ScriptedStarter([_created])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=None,
        served_project=PROJECT,
    )

    assert isinstance(outcome, QueueRunStarted)
    assert outcome.item_id == served_item.item_reference.item_id
    assert [binding.item_id for binding in queue.reserved] == [
        served_item.item_reference.item_id
    ]


@pytest.mark.parametrize("ending", [RunState.FAILED, RunState.CANCELLED])
def test_an_item_whose_run_ended_badly_is_released_and_started_again(
    ending: RunState,
) -> None:
    """The sweep that gives the binding back does not also start the item.

    Two sweeps, because that is what the runtime does: the first records the
    ending and returns the item to the start order one proposal revision on,
    and the second starts it under a run named after that new revision -- never
    the run that already ended.
    """

    ended = RunId("run-that-ended")
    item = _bound("gh:610", rank=1, run_id=ended)
    queue = _QueueRecording(
        QueueItemsPage((item,), None), endings={ended: QueueLaunchRunEnded(ending, 0)}
    )
    catalog = _CatalogResolverStub({LINEAGE: REVISION_HASH})
    starter = _ScriptedStarter([_created])
    tracker = _tracker_listing(("gh:610", True))

    (released,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=None,
        served_project=PROJECT,
        tracker=tracker,
    )
    (started,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=None,
        served_project=PROJECT,
        tracker=tracker,
    )

    assert item.launch_binding is not None
    assert released == QueueItemRestarting(
        item.item_reference.item_id, item.launch_binding, ending, 1
    )
    assert isinstance(started, QueueRunStarted)
    assert started.binding.run_id != ended
    assert started.binding.proposal_revision == QueueProjectionRevision(2)


def test_a_completed_run_keeps_its_item_bound_and_releases_nothing() -> None:
    """A completed run is the item's answer; a second one would pay twice.

    The tracker here has no listing arranged, so asking it would fail: only a
    launch that could be restarted ever asks the tracker.
    """

    completed = RunId("run-that-answered")
    item = _bound("gh:620", rank=1, run_id=completed)
    queue = _QueueRecording(
        QueueItemsPage((item,), None),
        endings={completed: QueueLaunchRunEnded(RunState.COMPLETED, 0)},
    )
    catalog = _CatalogResolverStub({LINEAGE: REVISION_HASH})
    starter = _ScriptedStarter(
        [
            DurableRunExisting(
                Run(
                    completed,
                    WorkflowRevisionHash(REVISION_HASH.value),
                    RunState.COMPLETED,
                    "final",
                    0,
                    0,
                    Sha256Hash("f" * 64),
                )
            )
        ]
    )

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=None,
        served_project=PROJECT,
        tracker=FakeTrackerItemSource(),
    )

    assert queue.released == []
    assert isinstance(outcome, QueueRunAlreadyActive)
    assert outcome.binding.run_id == completed


@pytest.mark.parametrize(
    ("spent", "releases"),
    [(MAXIMUM_QUEUE_LAUNCH_RESTARTS - 1, True), (MAXIMUM_QUEUE_LAUNCH_RESTARTS, False)],
)
def test_an_item_is_restarted_up_to_the_cap_and_then_stays_bound(
    spent: int, releases: bool
) -> None:
    """Money is spent per restart, so the last ending is where the item stops."""

    ended = RunId("run-that-kept-failing")
    item = _bound("gh:630", rank=1, run_id=ended)
    queue = _QueueRecording(
        QueueItemsPage((item,), None),
        endings={ended: QueueLaunchRunEnded(RunState.FAILED, spent)},
    )
    catalog = _CatalogResolverStub({LINEAGE: REVISION_HASH})

    (outcome,) = advance_queue(
        queue,
        catalog,
        _ScriptedStarter([]),
        workflow_document_parser=None,
        served_project=PROJECT,
        tracker=_tracker_listing(("gh:630", True)),
    )

    assert bool(queue.released) is releases
    assert item.launch_binding is not None
    assert outcome == (
        QueueItemRestarting(
            item.item_reference.item_id, item.launch_binding, RunState.FAILED, spent + 1
        )
        if releases
        else QueueItemRestartsExhausted(
            item.item_reference.item_id, item.launch_binding, RunState.FAILED, spent
        )
    )


def _withheld_restart(
    tracker: FakeTrackerItemSource,
    caplog: pytest.LogCaptureFixture,
    *,
    policy: QueueProjectPolicyRevision | None = POLICY,
    spent: int = 0,
) -> tuple[QueueItemSnapshot, _QueueRecording, QueueItemRestartWithheld]:
    """One sweep over an item whose FAILED run the tracker no longer lets restart."""

    ended = RunId("run-that-ended")
    item = _bound("gh:640", rank=1, run_id=ended)
    queue = _QueueRecording(
        QueueItemsPage((item,), None),
        endings={ended: QueueLaunchRunEnded(RunState.FAILED, spent)},
        policy=policy,
    )
    starter = _ScriptedStarter([])
    with caplog.at_level(logging.INFO, logger="atelier2"):
        (outcome,) = advance_queue(
            queue,
            _CatalogResolverStub({LINEAGE: REVISION_HASH}),
            starter,
            workflow_document_parser=None,
            served_project=PROJECT,
            tracker=tracker,
        )
    assert isinstance(outcome, QueueItemRestartWithheld)
    assert queue.released == []
    assert starter.asks == []
    assert queue.page.items == (item,)
    return item, queue, outcome


@pytest.mark.parametrize(
    ("tracker", "refusal"),
    [
        pytest.param(
            _tracker_listing(),
            QueueRestartRefusal.TRACKER_ITEM_CLOSED,
            id="tracker item closed",
        ),
        pytest.param(
            _tracker_listing(("gh:640", False)),
            QueueRestartRefusal.LABEL_REMOVED,
            id="label removed",
        ),
    ],
)
def test_an_ended_run_is_not_restarted_once_the_tracker_withdraws_its_authority(
    tracker: FakeTrackerItemSource,
    refusal: QueueRestartRefusal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Closed, or open without the label: the binding stays as it ended.

    Nothing is written and nothing is started; the item stays admitted and
    bound to its ended run, and the sweep names why in its outcome and in the
    journal, the same words for an operator as for the next sweep.
    """

    item, _queue, outcome = _withheld_restart(tracker, caplog)

    assert item.launch_binding is not None
    assert outcome == QueueItemRestartWithheld(
        item.item_reference.item_id, item.launch_binding, RunState.FAILED, refusal
    )
    assert [
        (getattr(record, "item_id", None), getattr(record, "refusal", None))
        for record in caplog.records
        if getattr(record, "event", None) == "queue_restart_withheld"
    ] == [(item.item_reference.item_id.value, refusal.value)]


def test_an_unreadable_tracker_restarts_nothing_and_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Soft, like the admission: no durable row moves, and the journal says why."""

    unreadable = TrackerSourceUnavailable("the tracker refused the listing")

    _item, _queue, outcome = _withheld_restart(
        FakeTrackerItemSource(open_items_answer=unreadable), caplog
    )

    assert outcome.refusal is QueueRestartRefusal.TRACKER_UNREADABLE
    assert [
        (record.levelno, getattr(record, "detail", None))
        for record in caplog.records
        if getattr(record, "event", None) == "queue_restart_source_unreadable"
    ] == [(logging.WARNING, unreadable.detail)]


@pytest.mark.parametrize(
    ("policy", "tracker"),
    [
        pytest.param(None, _tracker_listing(("gh:640", True)), id="no policy"),
        pytest.param(
            QueueProjectPolicyRevision(PROJECT, 1, 5, None),
            _tracker_listing(("gh:640", True)),
            id="policy names no label",
        ),
        pytest.param(POLICY, None, id="no tracker connected"),
    ],
)
def test_an_instance_with_no_label_to_restart_under_restarts_nothing(
    policy: QueueProjectPolicyRevision | None,
    tracker: FakeTrackerItemSource | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a label there is no unattended authority to spend on a restart."""

    ended = RunId("run-that-ended")
    item = _bound("gh:640", rank=1, run_id=ended)
    queue = _QueueRecording(
        QueueItemsPage((item,), None),
        endings={ended: QueueLaunchRunEnded(RunState.FAILED, 0)},
        policy=policy,
    )

    (outcome,) = advance_queue(
        queue,
        _CatalogResolverStub({LINEAGE: REVISION_HASH}),
        _ScriptedStarter([]),
        workflow_document_parser=None,
        served_project=PROJECT,
        tracker=tracker,
    )

    assert isinstance(outcome, QueueItemRestartWithheld)
    assert outcome.refusal is QueueRestartRefusal.AUTOMATION_LABEL_UNSET
    assert queue.released == []


def test_the_tracker_is_asked_before_the_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A closed item at the cap is withheld, not exhausted: the check comes first."""

    _item, _queue, outcome = _withheld_restart(
        _tracker_listing(), caplog, spent=MAXIMUM_QUEUE_LAUNCH_RESTARTS
    )

    assert outcome.refusal is QueueRestartRefusal.TRACKER_ITEM_CLOSED


def test_one_sweep_reads_the_tracker_once_for_every_ended_launch() -> None:
    """The listing is one read per sweep, shared by every item that asks."""

    first_ended, second_ended = RunId("first-ended"), RunId("second-ended")
    first = _bound("gh:650", rank=1, run_id=first_ended)
    second = _bound("gh:651", rank=2, run_id=second_ended)
    queue = _QueueRecording(
        QueueItemsPage((first, second), None),
        endings={
            first_ended: QueueLaunchRunEnded(RunState.FAILED, 0),
            second_ended: QueueLaunchRunEnded(RunState.CANCELLED, 0),
        },
    )
    reads = 0

    class _CountingTracker(FakeTrackerItemSource):
        def open_items(self) -> OpenTrackerItemsObserved:
            nonlocal reads
            reads += 1
            listing = super().open_items()
            assert isinstance(listing, OpenTrackerItemsObserved)
            return listing

    tracker = _CountingTracker(
        open_items_answer=_tracker_listing(("gh:650", True)).open_items_answer
    )

    restarted, withheld = advance_queue(
        queue,
        _CatalogResolverStub({LINEAGE: REVISION_HASH}),
        _ScriptedStarter([]),
        workflow_document_parser=None,
        served_project=PROJECT,
        tracker=tracker,
    )

    assert reads == 1
    assert isinstance(restarted, QueueItemRestarting)
    assert isinstance(withheld, QueueItemRestartWithheld)
    assert withheld.refusal is QueueRestartRefusal.TRACKER_ITEM_CLOSED
    assert [command.binding.item_id for command in queue.released] == [
        first.item_reference.item_id
    ]
