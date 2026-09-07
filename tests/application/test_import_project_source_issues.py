"""The import use case: tracker answers become queue observations in this layer's words."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Never

import pytest

from atelier2.application.import_project_source_issues import (
    ProjectSourceIssuesImported,
    ProjectSourceNotConnected,
    SourcePayloadMalformed,
    import_project_source_issues,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    QueueItemId,
    QueueItemTrackerObservation,
    QueueLaunchBinding,
    ReleaseQueueLaunch,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    ObserveOpenTrackerItemsResult,
    OpenTrackerItemsObserved,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueItemsReconciled,
    QueueLaunchReleased,
    QueueLaunchRunOpen,
    ReconcileQueueItemsResult,
)
from tests.scenarios.issue_observation import FakeTrackerItemSource

PROJECT = ProjectId("studio")
OBSERVED_AT = RecordedAt("2026-09-01T09:00:00Z")


@dataclass
class _QueueRecording:
    """The queue projection reduced to what an import may do with it."""

    answer: ReconcileQueueItemsResult
    reconciliations: list[
        tuple[
            ProjectId,
            tuple[tuple[WorkItemReference, QueueItemTrackerObservation], ...],
            RecordedAt,
        ]
    ] = field(default_factory=list)
    released: list[ReleaseQueueLaunch] = field(default_factory=list)

    def reconcile_open_items(
        self,
        project: ProjectId,
        items: tuple[tuple[WorkItemReference, QueueItemTrackerObservation], ...],
        observed_at: RecordedAt,
    ) -> ReconcileQueueItemsResult:
        self.reconciliations.append((project, items, observed_at))
        return self.answer

    def plan(self, command: object) -> Never:
        raise AssertionError("an import never plans")

    def confirm(self, command: object) -> Never:
        raise AssertionError("an import never confirms an admission")

    def put_policy(self, policy: object, expected_revision: object) -> Never:
        raise AssertionError("an import never publishes a policy")

    def current_policy(self, project: object) -> Never:
        raise AssertionError("an import never reads the policy")

    def reserve_launch(self, binding: object) -> Never:
        raise AssertionError("an import never reserves a launch")

    def read_launch(self, binding: QueueLaunchBinding) -> QueueLaunchRunOpen:
        return QueueLaunchRunOpen()

    def release_launch(self, command: ReleaseQueueLaunch) -> QueueLaunchReleased:
        self.released.append(command)
        return QueueLaunchReleased()

    def list_items(self, after: object, limit: object) -> Never:
        raise AssertionError("an import never reads the projection")


def _listing(*items: tuple[str, str]) -> FakeTrackerItemSource:
    return FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            tuple(
                ObservedOpenTrackerItem(TrackerItemReference(reference), title, ())
                for reference, title in items
            ),
            OBSERVED_AT,
        )
    )


def _item_ids(*references: str) -> tuple[QueueItemId, ...]:
    return tuple(
        WorkItemReference(PROJECT, TrackerItemReference(reference)).item_id
        for reference in references
    )


def test_the_open_items_reach_the_queue_with_their_titles_and_the_runs_read_time() -> (
    None
):
    observed = _item_ids("gh:79", "gh:652")
    queue = _QueueRecording(QueueItemsReconciled(observed, observed[:1], ()))

    outcome = import_project_source_issues(
        PROJECT,
        _listing(("gh:79", "First listed item"), ("gh:652", "Second listed item")),
        queue,
    )

    assert outcome == ProjectSourceIssuesImported(observed=2, newly_observed=1)
    assert queue.reconciliations == [
        (
            PROJECT,
            (
                (
                    WorkItemReference(PROJECT, TrackerItemReference("gh:79")),
                    QueueItemTrackerObservation("First listed item", OBSERVED_AT),
                ),
                (
                    WorkItemReference(PROJECT, TrackerItemReference("gh:652")),
                    QueueItemTrackerObservation("Second listed item", OBSERVED_AT),
                ),
            ),
            OBSERVED_AT,
        )
    ]


@pytest.mark.parametrize(
    "title",
    ["", "x" * (MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS + 1)],
    ids=["empty", "overlong"],
)
def test_a_title_the_projection_cannot_hold_is_refused_before_anything_is_written(
    title: str,
) -> None:
    queue = _QueueRecording(QueueItemsReconciled((), (), ()))

    outcome = import_project_source_issues(
        PROJECT, _listing(("gh:79", "First listed item"), ("gh:652", title)), queue
    )

    assert isinstance(outcome, SourcePayloadMalformed)
    assert "gh:652" in outcome.detail
    assert queue.reconciliations == []


@pytest.mark.parametrize(
    ("project", "source"),
    [(None, _listing()), (PROJECT, None)],
)
def test_an_unconnected_instance_is_refused_by_name_without_touching_the_queue(
    project: ProjectId | None, source: FakeTrackerItemSource | None
) -> None:
    queue = _QueueRecording(QueueItemsReconciled((), (), ()))

    outcome = import_project_source_issues(project, source, queue)

    assert outcome == ProjectSourceNotConnected()
    assert queue.reconciliations == []


@pytest.mark.parametrize(
    ("source_answer", "expected"),
    [
        (
            TrackerSourceUnavailable("GitHub answered 503"),
            ReadUnavailable("GitHub answered 503"),
        ),
        (
            TrackerPayloadMalformed("no numbers"),
            SourcePayloadMalformed("no numbers"),
        ),
    ],
)
def test_a_tracker_refusal_is_translated_and_writes_nothing(
    source_answer: ObserveOpenTrackerItemsResult, expected: object
) -> None:
    queue = _QueueRecording(QueueItemsReconciled((), (), ()))

    outcome = import_project_source_issues(
        PROJECT, FakeTrackerItemSource(open_items_answer=source_answer), queue
    )

    assert outcome == expected
    assert queue.reconciliations == []


@pytest.mark.parametrize(
    ("store_answer", "expected"),
    [
        (DurableWriteUnavailable(), WriteUnavailable()),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ],
)
def test_a_store_failure_is_translated_into_this_layers_word(
    store_answer: ReconcileQueueItemsResult, expected: object
) -> None:
    outcome = import_project_source_issues(
        PROJECT, _listing(("gh:1", "Listed item")), _QueueRecording(store_answer)
    )

    assert outcome == expected
