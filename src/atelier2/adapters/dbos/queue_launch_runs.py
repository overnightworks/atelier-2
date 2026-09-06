"""Where a launch binding meets the run it reserved.

Reserving one belongs to the projection store, which needs the whole item to
decide it. What follows the reservation lives here: how many launches of a
project are still going, how the run one binding named stands, and the single
transaction that gives an item back to the sweep when that run ended badly.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.queue_tables import (
    queue_dependency_edges,
    queue_items,
    queue_launch_bindings,
    queue_proposal_revisions,
)
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QueueItemId,
    QueueItemState,
    QueueLaunchBinding,
    QueueProjectionRevision,
    ReleaseQueueLaunch,
)
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.queue_projection import (
    QueueLaunchReleased,
    QueueLaunchReleaseRefused,
    QueueLaunchRunEnded,
    QueueLaunchRunOpen,
    QueueReadUnavailable,
    ReadQueueLaunchResult,
    ReleaseQueueLaunchResult,
)


class DurableQueueReleaseIncomplete(RuntimeError):
    """A released binding did not leave the item where the release decided it."""


HELD_BINDING = queue_launch_bindings.c.ended_run_state.is_(None)
"""The one binding an item holds now: every earlier one names how its run ended."""


def held_binding(
    connection: Connection, item_id: QueueItemId
) -> QueueLaunchBinding | None:
    """The one binding this item holds now, or `None` while it holds none."""

    record = (
        connection.execute(
            sa.select(queue_launch_bindings).where(
                queue_launch_bindings.c.item_id == item_id.value, HELD_BINDING
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    return QueueLaunchBinding(
        QueueItemId(str(record["item_id"])),
        QueueProjectionRevision(int(record["proposal_revision"])),
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["workflow_revision_hash"])),
    )


def active_launch_count(connection: Connection, project: ProjectId) -> int:
    """How many of this project's held launches have not reached an ending."""

    count = connection.scalar(
        sa.select(sa.func.count())
        .select_from(
            queue_launch_bindings.outerjoin(
                runs, queue_launch_bindings.c.run_id == runs.c.run_id
            )
        )
        .where(
            queue_launch_bindings.c.project_id == project.value,
            HELD_BINDING,
            sa.or_(
                runs.c.run_id.is_(None),
                runs.c.state.not_in(
                    tuple(state.value for state in TERMINAL_RUN_STATES)
                ),
            ),
        )
    )
    if count is None:
        raise ValueError("active queue launch count could not be read")
    return int(count)


def restarts_spent(connection: Connection, item_id: QueueItemId) -> int:
    """How many of this item's launches were given back, which is its next ordinal."""

    count = connection.scalar(
        sa.select(sa.func.count()).where(
            queue_launch_bindings.c.item_id == item_id.value,
            queue_launch_bindings.c.ended_run_state.is_not(None),
        )
    )
    if count is None:
        raise ValueError("released queue launch count could not be read")
    return int(count)


def read_launch(engine: Engine, binding: QueueLaunchBinding) -> ReadQueueLaunchResult:
    """How the run this binding named stands, and what the item already spent."""

    try:
        with engine.connect() as connection:
            record = (
                connection.execute(
                    sa.select(queue_launch_bindings.c.restart_ordinal, runs.c.state)
                    .select_from(
                        queue_launch_bindings.outerjoin(
                            runs, queue_launch_bindings.c.run_id == runs.c.run_id
                        )
                    )
                    # A binding is keyed by its item and revision, and its run id
                    # is unique, so this names exactly the one row asked about.
                    .where(queue_launch_bindings.c.run_id == binding.run_id.value)
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                raise ValueError("a held launch binding has left the store")
            if record["state"] is None:
                return QueueLaunchRunOpen()
            state = RunState(str(record["state"]))
            if state not in TERMINAL_RUN_STATES:
                return QueueLaunchRunOpen()
            return QueueLaunchRunEnded(state, int(record["restart_ordinal"]))
    except (OperationalError, PoolTimeoutError):
        return QueueReadUnavailable()
    except (ValueError, DatabaseError):
        return DurableStateCorrupt()


def release_ended_launch(
    engine: Engine, command: ReleaseQueueLaunch
) -> ReleaseQueueLaunchResult:
    """Give one item back to the sweep, or leave every durable row untouched.

    The binding's own ending is the compare-and-set: only the sweep whose
    update finds it still held writes one, and a second sweep deciding against
    the same rows a moment earlier changes nothing. Everything the release
    writes afterwards -- the proposal carried forward, the prerequisites it
    named, the item's advance -- stands or falls with that one transaction, so
    a store that refuses any of it leaves the item exactly as it was.
    """

    try:
        with canonical_write_transaction(engine) as connection:
            if _ended(connection, command) != 1:
                return QueueLaunchReleaseRefused()
            _carried_forward(connection, command.binding)
            if _readmitted(connection, command.binding) != 1:
                raise DurableQueueReleaseIncomplete(
                    "the released item did not stand at its own admitted revision"
                )
            return QueueLaunchReleased()
    except (OperationalError, PoolTimeoutError):
        return DurableWriteUnavailable()
    except (ValueError, RuntimeError, DatabaseError):
        return DurableStateCorrupt()


def _ended(connection: Connection, command: ReleaseQueueLaunch) -> int:
    return connection.execute(
        queue_launch_bindings.update()
        .where(
            queue_launch_bindings.c.item_id == command.binding.item_id.value,
            queue_launch_bindings.c.proposal_revision
            == command.binding.proposal_revision.value,
            queue_launch_bindings.c.run_id == command.binding.run_id.value,
            HELD_BINDING,
        )
        .values(ended_run_state=command.ended_state.value)
    ).rowcount


def _carried_forward(connection: Connection, binding: QueueLaunchBinding) -> None:
    """Re-issue the item's proposal one revision on, prerequisites and all.

    The decision itself does not change -- same workflow, same rank, same
    disposition, same author -- so the copy states no proposal nobody made; the
    revision it stands at is what makes the item's next run a different run.
    The prerequisites travel with it because they are read at the item's
    current revision, and a revision without them would start an item whose
    prerequisites nobody has met.
    """

    released = binding.proposal_revision.value
    successor = sa.literal(released + 1)
    connection.execute(
        queue_proposal_revisions.insert().from_select(
            [
                "item_id",
                "proposal_revision",
                "project_id",
                "priority_rank",
                "workflow_lineage_id",
                "automation_disposition",
                "policy_revision",
                "source",
            ],
            sa.select(
                queue_proposal_revisions.c.item_id,
                successor,
                queue_proposal_revisions.c.project_id,
                queue_proposal_revisions.c.priority_rank,
                queue_proposal_revisions.c.workflow_lineage_id,
                queue_proposal_revisions.c.automation_disposition,
                queue_proposal_revisions.c.policy_revision,
                queue_proposal_revisions.c.source,
            ).where(
                queue_proposal_revisions.c.item_id == binding.item_id.value,
                queue_proposal_revisions.c.proposal_revision == released,
            ),
        )
    )
    connection.execute(
        queue_dependency_edges.insert().from_select(
            ["item_id", "proposal_revision", "project_id", "prerequisite_item_id"],
            sa.select(
                queue_dependency_edges.c.item_id,
                successor,
                queue_dependency_edges.c.project_id,
                queue_dependency_edges.c.prerequisite_item_id,
            ).where(
                queue_dependency_edges.c.item_id == binding.item_id.value,
                queue_dependency_edges.c.proposal_revision == released,
            ),
        )
    )


def _readmitted(connection: Connection, binding: QueueLaunchBinding) -> int:
    """Advance the item onto the proposal revision the release just wrote."""

    return connection.execute(
        queue_items.update()
        .where(
            queue_items.c.item_id == binding.item_id.value,
            queue_items.c.state == QueueItemState.ADMITTED.value,
            queue_items.c.current_proposal_revision == binding.proposal_revision.value,
        )
        .values(
            current_proposal_revision=queue_items.c.current_proposal_revision + 1,
            state_version=queue_items.c.state_version + 1,
        )
    ).rowcount
