"""Import the connected tracker's open items as OBSERVED queue rows.

**Why this exists.** Since Slice 1 the queue could hold OBSERVED rows and the
admission door could advance them, but nothing in production ever created one:
an operator had to invent tracker references by hand. This module is the
caller that turns the connected repository's open issues into observed rows
(#652), plus the read that shows the operator what is now waiting for an
admission decision.

**Why here and not the route.** The composition owns which tracker the served
project is connected to; this layer owns only that one observation of that
tracker becomes one idempotent durable write, and that every port answer
becomes this layer's own vocabulary -- exactly as `admit_queue_item` translates
the same store's words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectSourceNotConnected,
    ReadUnavailable,
    SourcePayloadMalformed,
    WriteUnavailable,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import (
    QueueItemId,
    QueueItemTrackerObservation,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.issue_observation import (
    OpenTrackerItemsObserved,
    TrackerItemSource,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueItemsPage,
    QueueItemsReconciled,
    QueueProjection,
    QueueReadUnavailable,
)


@dataclass(frozen=True)
class SkippedProjectSourceItem:
    """An open tracker item this import did not observe, named so the rest still can."""

    reference: TrackerItemReference
    reason: str


@dataclass(frozen=True)
class ProjectSourceIssuesImported:
    """The observation landed: usable open issues are rows; unusable ones are named."""

    observed: int
    newly_observed: int
    skipped: tuple[SkippedProjectSourceItem, ...] = ()


type ImportProjectSourceIssuesOutcome = (
    ProjectSourceIssuesImported
    | ProjectSourceNotConnected
    | SourcePayloadMalformed
    | ReadUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)


def import_project_source_issues(
    project: ProjectId | None,
    source: TrackerItemSource | None,
    queue: QueueProjection,
) -> ImportProjectSourceIssuesOutcome:
    """Reconcile the queue with the tracker's open items, idempotently.

    Idempotency needs no cursor: every reference derives the same durable
    `QueueItemId`, so a repeated import rewrites nothing but the dated title
    observation and never touches an admission. What the tracker no longer
    lists leaves the open set in the same durable step -- the import derives
    that retirement rather than asking the tracker for a lifecycle (ADR 0016,
    2026-09-01 amendment).

    An item the run could not read is still in that open set: skip is not
    absence, so the last good observation is handed through rather than
    letting set-difference retire it.
    """

    if project is None or source is None:
        return ProjectSourceNotConnected()
    match source.open_items():
        case OpenTrackerItemsObserved() as listing:
            observed_at = listing.observed_at
            items: list[tuple[WorkItemReference, QueueItemTrackerObservation]] = []
            skipped: list[SkippedProjectSourceItem] = []
            for item in listing.items:
                try:
                    observation = QueueItemTrackerObservation(item.title, observed_at)
                except ValueError as refusal:
                    skipped.append(
                        SkippedProjectSourceItem(item.reference, str(refusal))
                    )
                    continue
                items.append((WorkItemReference(project, item.reference), observation))
            if skipped:
                kept = _still_open_skipped_observations(
                    queue, project, tuple(skipped), observed_at
                )
                if not isinstance(kept, tuple):
                    return kept
                items.extend(kept)
        case TrackerSourceUnavailable(detail):
            return ReadUnavailable(detail)
        case TrackerPayloadMalformed(detail):
            return SourcePayloadMalformed(detail)
        case _ as unreachable:
            assert_never(unreachable)
    match queue.reconcile_open_items(project, tuple(items), observed_at):
        case QueueItemsReconciled(observed, newly_observed, _):
            return ProjectSourceIssuesImported(
                len(observed), len(newly_observed), tuple(skipped)
            )
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _still_open_skipped_observations(
    queue: QueueProjection,
    project: ProjectId,
    skipped: tuple[SkippedProjectSourceItem, ...],
    observed_at: RecordedAt,
) -> (
    tuple[tuple[WorkItemReference, QueueItemTrackerObservation], ...]
    | ReadUnavailable
    | DurableStateCorrupt
):
    """Restate the last good title of a skipped row so set-difference does not retire it.

    `reconcile_open_items` treats the handed-in tuple as the whole open set:
    absence is retirement (ADR 0016). A skipped item is still in the tracker's
    listing, so it has not left the pullable set; the port has no third
    "leave untouched" answer, and an unusable title cannot form a fresh
    observation. The last readable title at this run's time is the only way
    the open set still names it.
    """

    wanted = {item.reference for item in skipped}
    found: dict[TrackerItemReference, QueueItemTrackerObservation] = {}
    after: QueueItemId | None = None
    while True:
        match queue.list_items(after, MAXIMUM_PAGE_ITEMS):
            case QueueItemsPage(page_items, next_after):
                for snapshot in page_items:
                    reference = snapshot.item_reference
                    observation = snapshot.observation
                    if (
                        reference.project == project
                        and reference.tracker_item in wanted
                        and snapshot.retired_at is None
                        and observation is not None
                    ):
                        found.setdefault(
                            reference.tracker_item,
                            QueueItemTrackerObservation(observation.title, observed_at),
                        )
                if next_after is None:
                    return tuple(
                        (
                            WorkItemReference(project, item.reference),
                            found[item.reference],
                        )
                        for item in skipped
                        if item.reference in found
                    )
                after = next_after
            case QueueReadUnavailable():
                return ReadUnavailable()
            case PortDurableStateCorrupt():
                return DurableStateCorrupt()
            case _ as unreachable:
                assert_never(unreachable)
