"""Plan a queue proposal, and read and write the project policy it is planned under."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    PlanQueueItem,
    QueueProjectPolicyRevision,
    QueueProposalOutcome,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.queue_projection import (
    QueuePlanner,
    QueuePolicyReader,
    QueuePolicyWriter,
    QueueReadUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyAbsent as PortQueueProjectPolicyAbsent,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyFound as PortQueueProjectPolicyFound,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyPublished as PortQueueProjectPolicyPublished,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyRevisionConflict as PortQueueProjectPolicyRevisionConflict,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyUnchanged as PortQueueProjectPolicyUnchanged,
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


@dataclass(frozen=True)
class QueueProjectPolicyRead:
    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyNotSet:
    """The project has published no policy revision yet (ADR 0016)."""


type PlanQueueItemOutcome = (
    QueueProposalOutcome | WriteUnavailable | DurableStateCorrupt
)
type PutQueueProjectPolicyOutcome = (
    QueueProjectPolicyPublished
    | QueueProjectPolicyUnchanged
    | QueueProjectPolicyRevisionConflict
    | WriteUnavailable
    | DurableStateCorrupt
)
type GetQueueProjectPolicyOutcome = (
    QueueProjectPolicyRead
    | QueueProjectPolicyNotSet
    | ReadUnavailable
    | DurableStateCorrupt
)


def plan_queue_item(
    command: PlanQueueItem, queue: QueuePlanner
) -> PlanQueueItemOutcome:
    result = queue.plan(command)
    if isinstance(result, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(result, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return result


def put_queue_project_policy(
    policy: QueueProjectPolicyRevision,
    expected_revision: int,
    queue: QueuePolicyWriter,
) -> PutQueueProjectPolicyOutcome:
    result = queue.put_policy(policy, expected_revision)
    if isinstance(result, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(result, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    if isinstance(result, PortQueueProjectPolicyPublished):
        return QueueProjectPolicyPublished(result.policy)
    if isinstance(result, PortQueueProjectPolicyUnchanged):
        return QueueProjectPolicyUnchanged(result.policy)
    if isinstance(result, PortQueueProjectPolicyRevisionConflict):
        return QueueProjectPolicyRevisionConflict(
            result.expected_revision, result.actual_revision
        )
    raise AssertionError("queue policy writer returned an unknown outcome")


def get_queue_project_policy(
    project: ProjectId, queue: QueuePolicyReader
) -> GetQueueProjectPolicyOutcome:
    match queue.current_policy(project):
        case PortQueueProjectPolicyFound(policy):
            return QueueProjectPolicyRead(policy)
        case PortQueueProjectPolicyAbsent():
            return QueueProjectPolicyNotSet()
        case QueueReadUnavailable():
            return ReadUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
