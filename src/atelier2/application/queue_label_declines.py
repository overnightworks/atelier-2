"""Why the automation label leaves an item standing instead of admitting it.

Every reason here is a decline the sweep reports in its own words, and the
item stays exactly as durable as it was. The newest reason asks the claim
ledger: the claim door remains the authority over who may build where, but an
item admitted into a collision spends a run and one of its two restart marks
on a refusal that was certain before the run existed, so the sweep leaves it
standing and asks again next tick, when the other lane may have released.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import PurePosixPath
from typing import Protocol

from atelier2.contracts.definition_sources import MAXIMUM_REPOSITORY_PATH_CHARACTERS
from atelier2.contracts.queue_projection import (
    QueueAdmissionOutcome,
    QueueItemId,
    TrackerItemReference,
)
from atelier2.contracts.work_items import WorkItemScope
from atelier2.ports.work_item_claims import ClaimTouch


class ExclusiveLaneOccupancy(Protocol):
    """The claim ledger, asked which lanes other than this item's own it holds."""

    def foreign_claims(
        self, tracker_item: TrackerItemReference
    ) -> tuple[ClaimTouch, ...]: ...


@dataclass(frozen=True, slots=True)
class ClaimCollision:
    """One foreign claim, and the path of this item's scope that it covers."""

    claim: ClaimTouch
    path: PurePosixPath

    @property
    def detail(self) -> str:
        """How the decline names it: the other lane, then the path it stands on."""

        path = self.path.as_posix()[:MAXIMUM_REPOSITORY_PATH_CHARACTERS]
        return f"{self.claim.lane} on {path}"


@dataclass(frozen=True)
class QueueLabelAdmissionScopeMissing:
    """The labelled item's body names no scope list, so the rule does not admit it."""


@dataclass(frozen=True)
class QueueLabelAdmissionScopeMalformed:
    """A scope-list line is not a relative path, so the rule does not admit the item."""

    token: str


@dataclass(frozen=True)
class QueueLabelAdmissionTrackerItemUnknown:
    """The tracker does not know this labelled item, so the rule does not admit it."""


@dataclass(frozen=True)
class QueueLabelAdmissionTouchesAnotherLane:
    """The labelled item's scope touches a path another lane's claim covers."""

    collision: ClaimCollision


@dataclass(frozen=True)
class QueueLabelAdmissionDeclined:
    """One labelled item the projection did not newly admit, in its own words."""

    item_id: QueueItemId
    outcome: (
        QueueAdmissionOutcome
        | QueueLabelAdmissionScopeMissing
        | QueueLabelAdmissionScopeMalformed
        | QueueLabelAdmissionTrackerItemUnknown
        | QueueLabelAdmissionTouchesAnotherLane
    )


def queue_label_admission_declined_reason(outcome: object) -> str:
    """One line for the operator: what declined the item, and what names it."""

    name = type(outcome).__name__
    if isinstance(outcome, QueueLabelAdmissionTouchesAnotherLane):
        return f"{name}: {outcome.collision.detail}"
    if not isinstance(outcome, QueueLabelAdmissionScopeMalformed):
        return name
    return f"{name}: {' '.join(outcome.token.split())[:MAXIMUM_REPOSITORY_PATH_CHARACTERS]}"


def colliding_lane(
    occupancy: ExclusiveLaneOccupancy | None,
    tracker_item: TrackerItemReference,
    scope: WorkItemScope,
) -> QueueLabelAdmissionTouchesAnotherLane | None:
    """Why this scope may not be admitted yet, or nothing when it may.

    `None` for an unasked ledger too: without one the admission decides as it
    always did, and the door still refuses what it must.
    """

    if occupancy is None:
        return None
    collision = _covering_claim(occupancy.foreign_claims(tracker_item), scope)
    if collision is None:
        return None
    return QueueLabelAdmissionTouchesAnotherLane(collision)


def _covering_claim(
    claims: tuple[ClaimTouch, ...], scope: WorkItemScope
) -> ClaimCollision | None:
    ours = tuple(PurePosixPath(path) for path in scope.paths)
    for claim in claims:
        for mine, held in product(ours, claim.scope):
            covered = _covered_path(mine, held)
            if covered is not None:
                return ClaimCollision(claim, covered)
    return None


def _covered_path(mine: PurePosixPath, held: PurePosixPath) -> PurePosixPath | None:
    """The narrower of two paths where one contains the other, else nothing.

    A claim on a directory covers every file under it, the way the claim door
    compares a granted claim with the lanes it touches.
    """

    if mine.is_relative_to(held):
        return mine
    if held.is_relative_to(mine):
        return held
    return None
