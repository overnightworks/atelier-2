"""What the queue sweep reads before it decides, and how those reads fail.

The sweep's decisions -- admit what the label names, give an ended launch
back, start what is admitted -- share their reads of durable and tracker
truth: the project's policy, the projection page by page with every snapshot
re-validated, and the tracker's open set keyed the way the queue keys its
items. They share what an untrustworthy read means too, so the two failures
live here with the reads that raise them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property
from typing import assert_never

from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QueueItemId,
    QueueItemSnapshot,
    QueueLaunchBinding,
    QueueProjectPolicyRevision,
    QueueRestartRefusal,
    WorkItemReference,
)
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
    QueueItemsReader,
    QueueLaunchAlreadyBound,
    QueueLaunchBlocked,
    QueueLaunchReserved,
    QueueLaunchReserver,
    QueuePolicyReader,
    QueueProjectPolicyAbsent,
    QueueProjectPolicyFound,
    QueueReadUnavailable,
)

_LOG = logging.getLogger("atelier2")


class QueueAdvanceUnavailable(RuntimeError):
    """Durable queue or catalog truth could not be read safely."""


class QueueAdvanceCorrupt(RuntimeError):
    """Durable queue, catalog, or run truth contradicted its contract."""


def active_policy(
    queue: QueuePolicyReader, project: ProjectId
) -> QueueProjectPolicyRevision | None:
    match queue.current_policy(project):
        case QueueProjectPolicyFound(policy):
            return policy
        case QueueProjectPolicyAbsent():
            return None
        case QueueReadUnavailable():
            raise QueueAdvanceUnavailable("the queue policy could not be read")
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt("the queue policy is corrupt and cannot be read")
        case _ as unreachable:
            assert_never(unreachable)


def projected_items(
    queue: QueueItemsReader, page_limit: int
) -> tuple[QueueItemSnapshot, ...]:
    """Every item of the whole projection, page by page, each re-validated."""

    items: list[QueueItemSnapshot] = []
    after: QueueItemId | None = None
    while True:
        page = queue.list_items(after, page_limit)
        if isinstance(page, QueueReadUnavailable):
            raise QueueAdvanceUnavailable("the queue could not be read for the sweep")
        if isinstance(page, PortDurableStateCorrupt):
            raise QueueAdvanceCorrupt("the queue is corrupt and cannot be swept")
        if not isinstance(page, QueueItemsPage):
            raise QueueAdvanceCorrupt("the queue answered an unknown projection")
        items.extend(validated_snapshot(item) for item in page.items)
        if page.next_after is None:
            return tuple(items)
        after = page.next_after


def validated_snapshot(item: QueueItemSnapshot) -> QueueItemSnapshot:
    """Re-run the snapshot's own validation, translating a refusal to the sweep's."""

    try:
        return item.revalidated()
    except (AttributeError, TypeError, ValueError) as error:
        raise QueueAdvanceCorrupt(
            "the queue projection returned an inconsistent item"
        ) from error


def reserved_launch(
    queue: QueueLaunchReserver, binding: QueueLaunchBinding
) -> QueueLaunchBinding | QueueItemSnapshot:
    """The binding the item now holds, or the item as the store blocked it."""

    match queue.reserve_launch(binding):
        case QueueLaunchReserved(binding=held) | QueueLaunchAlreadyBound(binding=held):
            return held
        case QueueLaunchBlocked(item=blocked):
            return validated_snapshot(blocked)
        case DurableWriteUnavailable():
            raise QueueAdvanceUnavailable("the launch reservation could not commit")
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt("the launch reservation found corrupt state")
        case _:
            raise QueueAdvanceCorrupt(
                "the queue answered an unknown launch reservation outcome"
            )


@dataclass(frozen=True)
class OpenTrackerItems:
    """One reading of the tracker's open set, keyed the way the queue keys items."""

    open: frozenset[QueueItemId]
    labelled: frozenset[QueueItemId]


def open_tracker_items(
    tracker: TrackerItemSource, project: ProjectId, label: str
) -> OpenTrackerItems | TrackerSourceUnavailable | TrackerPayloadMalformed:
    """The tracker's open items, and which of them carry `label`, as of one read.

    The labels are read at the instant the caller decides rather than from a
    durable copy (REQ-QUEUE-08), and a tracker that does not answer is handed
    back in the port's own words for the caller to translate.
    """

    listing = tracker.open_items()
    if not isinstance(listing, OpenTrackerItemsObserved):
        return listing
    keyed = {
        WorkItemReference(project, item.reference).item_id: item
        for item in listing.items
    }
    return OpenTrackerItems(
        frozenset(keyed),
        frozenset(item_id for item_id, item in keyed.items() if label in item.labels),
    )


class RestartAuthority:
    """Which ended launches the tracker still lets this sweep restart.

    One reading per sweep, taken only when an ended launch first asks and from
    the same listing the label admission reads: a restart is an unattended,
    paid decision, so it needs the authority an automatic admission needs
    (REQ-QUEUE-08) -- the item open and carrying the label at the instant the
    sweep decides. A tracker that cannot be read withholds every restart of
    the sweep, softly, and is said once in the journal; an instance with no
    label to restart under says nothing, because that is a steady state.
    """

    def __init__(
        self,
        queue: QueuePolicyReader,
        project: ProjectId | None,
        tracker: TrackerItemSource | None,
    ) -> None:
        self._queue = queue
        self._project = project
        self._tracker = tracker

    def refusal_for(self, item_id: QueueItemId) -> QueueRestartRefusal | None:
        listing = self._listing
        if isinstance(listing, QueueRestartRefusal):
            return listing
        if item_id not in listing.open:
            return QueueRestartRefusal.TRACKER_ITEM_CLOSED
        if item_id not in listing.labelled:
            return QueueRestartRefusal.LABEL_REMOVED
        return None

    @cached_property
    def _listing(self) -> OpenTrackerItems | QueueRestartRefusal:
        if self._project is None or self._tracker is None:
            return QueueRestartRefusal.AUTOMATION_LABEL_UNSET
        policy = active_policy(self._queue, self._project)
        if policy is None or policy.automation_label is None:
            return QueueRestartRefusal.AUTOMATION_LABEL_UNSET
        listing = open_tracker_items(
            self._tracker, self._project, policy.automation_label
        )
        if isinstance(listing, TrackerSourceUnavailable | TrackerPayloadMalformed):
            _LOG.warning(
                "No ended queue launch is restarted this sweep: the tracker "
                "could not be read (%s).",
                listing.detail,
                extra={
                    "event": "queue_restart_source_unreadable",
                    "detail": listing.detail,
                },
            )
            return QueueRestartRefusal.TRACKER_UNREADABLE
        return listing
