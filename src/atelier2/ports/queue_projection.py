"""Narrow ports for the durable Phase-D queue projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionOutcome,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemTrackerObservation,
    QueueLaunchBinding,
    QueueProjectPolicyRevision,
    QueueProposalOutcome,
    ReleaseQueueLaunch,
    WorkItemReference,
)
from atelier2.contracts.runs import RunState
from atelier2.contracts.when import RecordedAt
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

type ConfirmQueueProposalResult = (
    QueueAdmissionOutcome | DurableWriteUnavailable | DurableStateCorrupt
)


type PlanQueueItemResult = (
    QueueProposalOutcome | DurableWriteUnavailable | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueProjectPolicyPublished:
    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyUnchanged:
    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyRevisionConflict:
    expected_revision: int
    actual_revision: int


type PutQueueProjectPolicyResult = (
    QueueProjectPolicyPublished
    | QueueProjectPolicyUnchanged
    | QueueProjectPolicyRevisionConflict
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueLaunchReserved:
    binding: QueueLaunchBinding


@dataclass(frozen=True)
class QueueLaunchAlreadyBound:
    binding: QueueLaunchBinding


@dataclass(frozen=True)
class QueueLaunchBlocked:
    item: QueueItemSnapshot


type ReserveQueueLaunchResult = (
    QueueLaunchReserved
    | QueueLaunchAlreadyBound
    | QueueLaunchBlocked
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueReadUnavailable:
    """The store could not answer this read, and a later attempt may succeed."""


@dataclass(frozen=True)
class QueueLaunchRunEnded:
    """The bound run reached an ending, after this many restarts of its item."""

    state: RunState
    restarts_spent: int


@dataclass(frozen=True)
class QueueLaunchRunOpen:
    """The bound run has not ended, so the binding stays exactly where it is.

    A reservation whose start never landed says this too: the store holds no
    run row for it yet, which is no ending either, and the sweep asks the
    starter again rather than giving back a binding that spent nothing.
    """


type ReadQueueLaunchResult = (
    QueueLaunchRunEnded
    | QueueLaunchRunOpen
    | QueueReadUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueLaunchReleased:
    """The ended run gave its binding back and the item stands one revision on."""


@dataclass(frozen=True)
class QueueLaunchReleaseRefused:
    """The durable rows no longer say what this release was decided against.

    Another sweep released the same binding first: nothing is written, and the
    next sweep decides again against rows it can see.
    """


type ReleaseQueueLaunchResult = (
    QueueLaunchReleased
    | QueueLaunchReleaseRefused
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueItemsReconciled:
    """How one reconciliation of a project's open set landed, item by item.

    Identity does the deduplication (`QueueItemId` derives from project and
    tracker reference), so a repeated observation is named, never rewritten:
    `observed` is every item the run handed in, `newly_observed` the rows it
    created, and `retired` the rows it derived out of the open set.
    """

    observed: tuple[QueueItemId, ...]
    newly_observed: tuple[QueueItemId, ...]
    retired: tuple[QueueItemId, ...]


type ReconcileQueueItemsResult = (
    QueueItemsReconciled | DurableWriteUnavailable | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueItemsPage:
    """One typed projection across every queue lifecycle state."""

    items: tuple[QueueItemSnapshot, ...]
    next_after: QueueItemId | None


type ListQueueItemsResult = QueueItemsPage | QueueReadUnavailable | DurableStateCorrupt


@dataclass(frozen=True)
class QueueProjectPolicyFound:
    """The revision that is currently in force for this project."""

    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyAbsent:
    """The project has published no policy revision, which is a legitimate state.

    It carries no cap and no automation rule (ADR 0016, operator ruling
    28.08.2026), rather than an invented default of either.
    """


type ReadQueueProjectPolicyResult = (
    QueueProjectPolicyFound
    | QueueProjectPolicyAbsent
    | QueueReadUnavailable
    | DurableStateCorrupt
)


class QueuePlanner(Protocol):
    def plan(self, command: PlanQueueItem) -> PlanQueueItemResult: ...


class QueueAdmissionConfirmer(Protocol):
    def confirm(self, command: ConfirmQueueProposal) -> ConfirmQueueProposalResult: ...


class QueuePolicyReader(Protocol):
    def current_policy(self, project: ProjectId) -> ReadQueueProjectPolicyResult: ...


class QueuePolicyWriter(Protocol):
    def put_policy(
        self, policy: QueueProjectPolicyRevision, expected_revision: int
    ) -> PutQueueProjectPolicyResult: ...


class QueueLaunchReserver(Protocol):
    def reserve_launch(
        self, binding: QueueLaunchBinding
    ) -> ReserveQueueLaunchResult: ...


class QueueLaunchRuns(Protocol):
    """Where a launch binding meets the run it reserved, once that run exists.

    The two answers a sweep needs about a launch it already holds: how the run
    stands, and the release of a binding whose run ended without an answer.
    """

    def read_launch(self, binding: QueueLaunchBinding) -> ReadQueueLaunchResult: ...

    def release_launch(
        self, command: ReleaseQueueLaunch
    ) -> ReleaseQueueLaunchResult: ...


class QueueItemsReader(Protocol):
    def list_items(
        self, after: QueueItemId | None, limit: int
    ) -> ListQueueItemsResult: ...


class QueueProjection(
    QueuePlanner,
    QueueAdmissionConfirmer,
    QueuePolicyReader,
    QueuePolicyWriter,
    QueueLaunchReserver,
    QueueLaunchRuns,
    QueueItemsReader,
    Protocol,
):
    """The complete durable home of the Phase-D queue lifecycle."""

    def reconcile_open_items(
        self,
        project: ProjectId,
        items: tuple[tuple[WorkItemReference, QueueItemTrackerObservation], ...],
        observed_at: RecordedAt,
    ) -> ReconcileQueueItemsResult:
        """Make the project's rows agree with one reading of its open set.

        One durable step, because the three writes it holds are one fact:
        every handed-in item exists and carries this run's dated title
        observation, an item observed again loses its retirement, and every
        other row of *this* project left the open set and is retired at the
        run's own `observed_at` (ADR 0016, 2026-09-01 amendment: closedness is
        derived by set difference at import, never observed). An empty open
        set therefore retires the whole project, which is what an empty
        tracker answer means. Another project's rows are untouched.

        Every item must name `project` and carry `observed_at`; a caller that
        breaks either states a fact the run cannot have observed, so the
        implementation raises `ValueError` rather than answering an outcome.
        """
        ...
