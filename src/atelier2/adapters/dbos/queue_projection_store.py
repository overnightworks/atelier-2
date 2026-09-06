from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.queue_projection_records import (
    policy_from_record,
    policy_row,
    proposal_from_record,
    proposal_row,
)
from atelier2.adapters.dbos.queue_tables import (
    queue_dependency_edges,
    queue_items,
    queue_launch_bindings,
    queue_project_policy_revisions,
    queue_proposal_revisions,
)
from atelier2.adapters.dbos.schema import catalog_lineages, runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS, MINIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import (
    QUEUE_PROJECTION_REVISION_OBSERVED,
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmission,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemProposed,
    QueueItemSnapshot,
    QueueItemState,
    QueueItemTrackerObservation,
    QueueLaunchBinding,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalRefusal,
    QueueProposalRefused,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.queue_projection import (
    ConfirmQueueProposalResult,
    PlanQueueItemResult,
    PutQueueProjectPolicyResult,
    QueueItemsPage,
    QueueItemsReconciled,
    QueueLaunchAlreadyBound,
    QueueLaunchBlocked,
    QueueLaunchReserved,
    QueueProjectPolicyAbsent,
    QueueProjectPolicyFound,
    QueueProjectPolicyPublished,
    QueueProjectPolicyRevisionConflict,
    QueueProjectPolicyUnchanged,
    QueueReadUnavailable,
    ReadQueueProjectPolicyResult,
    ReconcileQueueItemsResult,
    ReserveQueueLaunchResult,
)


class DurableQueueAdmissionConflict(RuntimeError):
    """Durable rows do not form the exact admission CAS transition expected."""


_UNSUCCESSFUL_TERMINAL_RUN_STATE_VALUES = frozenset(
    state.value for state in TERMINAL_RUN_STATES if state is not RunState.COMPLETED
)

# The SQL realization of `contracts.queue_projection.queue_start_order_key`: the
# rank lives on `queue_proposal_revisions.priority_rank`, joined by the item's
# current proposal revision, never a rank column on `queue_items` itself.
_QUEUE_START_ORDER_JOIN = queue_items.outerjoin(
    queue_proposal_revisions,
    sa.and_(
        queue_items.c.item_id == queue_proposal_revisions.c.item_id,
        queue_items.c.current_proposal_revision
        == queue_proposal_revisions.c.proposal_revision,
    ),
)
_QUEUE_START_ORDER_UNRANKED = queue_items.c.current_proposal_revision.is_(None)
_QUEUE_START_ORDER_RANK = sa.func.coalesce(queue_proposal_revisions.c.priority_rank, 0)
_QUEUE_START_ORDER_COLUMNS = (
    _QUEUE_START_ORDER_UNRANKED,
    _QUEUE_START_ORDER_RANK,
    queue_items.c.item_id,
)


def _snapshot_from_record(
    connection: Connection, record: Mapping[Any, Any]
) -> QueueItemSnapshot:
    item_reference = WorkItemReference(
        ProjectId(str(record["project_id"])),
        TrackerItemReference(str(record["tracker_item_reference"])),
    )
    if item_reference.item_id.value != record["item_id"]:
        raise ValueError("durable queue item id disagrees with its derived identity")
    state = QueueItemState(str(record["state"]))
    lineage_id = record["workflow_lineage_id"]
    rationale = record["admission_rationale"]
    proposal_revision = record["current_proposal_revision"]
    state_version = record["state_version"]
    decision_authority = record["decision_authority"]
    admission = None
    if state is not QueueItemState.ADMITTED:
        if (
            lineage_id is not None
            or rationale is not None
            or decision_authority is not None
        ):
            raise ValueError("a non-admitted queue item cannot carry admission fields")
        if state is QueueItemState.OBSERVED and proposal_revision is not None:
            raise ValueError("an observed queue item cannot carry a proposal revision")
        if state is QueueItemState.PROPOSED and state_version != proposal_revision:
            raise ValueError("a proposed queue item must name its current revision")
    else:
        if not isinstance(lineage_id, str) or not isinstance(rationale, str):
            raise ValueError(
                "an admitted queue item must carry its lineage and rationale"
            )
        legacy_admission = proposal_revision is None and decision_authority is None
        proposed_admission = (
            proposal_revision is not None and decision_authority is not None
        )
        if not legacy_admission and not proposed_admission:
            raise ValueError("an admitted queue item has a partial proposal decision")
        if proposed_admission and not isinstance(decision_authority, str):
            raise ValueError("an admitted queue item decision authority must be text")
        admission = QueueAdmission(
            CatalogLineageId(lineage_id),
            QueueAdmissionRationale(rationale),
        )
    proposal = None
    if proposal_revision is not None:
        proposal_record = (
            connection.execute(
                sa.select(queue_proposal_revisions).where(
                    queue_proposal_revisions.c.item_id == item_reference.item_id.value,
                    queue_proposal_revisions.c.proposal_revision
                    == int(proposal_revision),
                )
            )
            .mappings()
            .one_or_none()
        )
        if proposal_record is None:
            raise ValueError("queue item points to a missing proposal revision")
        prerequisites = tuple(
            QueueItemId(str(value))
            for value in connection.scalars(
                sa.select(queue_dependency_edges.c.prerequisite_item_id)
                .where(
                    queue_dependency_edges.c.item_id == item_reference.item_id.value,
                    queue_dependency_edges.c.proposal_revision
                    == int(proposal_revision),
                )
                .order_by(queue_dependency_edges.c.prerequisite_item_id)
            )
        )
        proposal = proposal_from_record(proposal_record, prerequisites)
        if admission is not None:
            admission = QueueAdmission(
                admission.workflow_lineage_id,
                admission.rationale,
                QueueDecisionAuthority(decision_authority),
                QueueProjectionRevision(int(proposal_revision)),
            )
    binding_record = (
        connection.execute(
            sa.select(queue_launch_bindings).where(
                queue_launch_bindings.c.item_id == item_reference.item_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    launch_binding = (
        None
        if binding_record is None
        else QueueLaunchBinding(
            item_reference.item_id,
            QueueProjectionRevision(int(binding_record["proposal_revision"])),
            RunId(str(binding_record["run_id"])),
            WorkflowRevisionHash(str(binding_record["workflow_revision_hash"])),
        )
    )
    blockers = _blockers_for(
        connection,
        item_reference,
        state,
        proposal,
        launch_binding,
    )
    observed_title = record["observed_title"]
    title_observed_at = record["title_observed_at"]
    if (observed_title is None) != (title_observed_at is None):
        raise ValueError("a queue item observation must pair its title and marker")
    observation = (
        None
        if observed_title is None
        else QueueItemTrackerObservation(
            str(observed_title), RecordedAt(str(title_observed_at))
        )
    )
    retired_at = record["retired_at"]
    return QueueItemSnapshot(
        item_reference,
        state,
        QueueProjectionRevision(int(state_version)),
        admission,
        proposal,
        launch_binding,
        blockers,
        observation,
        None if retired_at is None else RecordedAt(str(retired_at)),
    )


def _blockers_for(
    connection: Connection,
    item_reference: WorkItemReference,
    state: QueueItemState,
    proposal: QueueProposal | None,
    launch_binding: QueueLaunchBinding | None,
) -> tuple[QueueBlockerKind, ...]:
    if state is QueueItemState.OBSERVED:
        return (QueueBlockerKind.PRIORITY_UNSET,)
    if proposal is None:
        if state is QueueItemState.ADMITTED:
            return (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)
        raise ValueError("a Phase-D queue state must carry its proposal")
    if state is QueueItemState.PROPOSED:
        return (
            (QueueBlockerKind.HUMAN_REQUIRED,)
            if proposal.automation_disposition
            is QueueAutomationDisposition.HUMAN_REQUIRED
            else ()
        )
    if launch_binding is not None:
        return ()
    dependency_states = connection.execute(
        sa.select(runs.c.state)
        .select_from(
            queue_dependency_edges.outerjoin(
                queue_launch_bindings,
                queue_dependency_edges.c.prerequisite_item_id
                == queue_launch_bindings.c.item_id,
            ).outerjoin(runs, queue_launch_bindings.c.run_id == runs.c.run_id)
        )
        .where(
            queue_dependency_edges.c.item_id == item_reference.item_id.value,
            queue_dependency_edges.c.proposal_revision
            == proposal_revision_for(connection, item_reference.item_id),
        )
    ).scalars()
    open_prerequisite = False
    failed_prerequisite = False
    for value in dependency_states:
        if value is None:
            open_prerequisite = True
            continue
        prerequisite_state = RunState(str(value))
        if prerequisite_state.value in _UNSUCCESSFUL_TERMINAL_RUN_STATE_VALUES:
            failed_prerequisite = True
        elif prerequisite_state is not RunState.COMPLETED:
            open_prerequisite = True
    if failed_prerequisite:
        return (QueueBlockerKind.PREREQUISITE_FAILED,)
    if open_prerequisite:
        return (QueueBlockerKind.PREREQUISITE_OPEN,)
    return ()


def proposal_revision_for(connection: Connection, item_id: QueueItemId) -> int:
    value = connection.scalar(
        sa.select(queue_items.c.current_proposal_revision).where(
            queue_items.c.item_id == item_id.value
        )
    )
    if value is None:
        raise ValueError("queue item has no current proposal")
    return int(value)


class DbosQueueProjectionStore:
    """The V44 queue policy, proposal, admission, and launch-binding store."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def reconcile_open_items(
        self,
        project: ProjectId,
        items: tuple[tuple[WorkItemReference, QueueItemTrackerObservation], ...],
        observed_at: RecordedAt,
    ) -> ReconcileQueueItemsResult:
        """Insert, re-date, un-retire, and retire one project's rows in one step.

        The retirement is a set difference over this project's rows alone, so a
        run for one project never touches another's. `INSERT OR IGNORE` on the
        derived `item_id` keeps a repeated import from creating a twin or
        rewinding an admission; the observation write that follows it touches
        only the three ADR 0016 columns, never `state`, `state_version`, or any
        admission field.
        """

        for reference, observation in items:
            if reference.project != project:
                raise ValueError("a reconciled item must belong to the named project")
            if observation.observed_at != observed_at:
                raise ValueError("a reconciled item must carry the run's read time")
        observed = tuple(reference.item_id for reference, _ in items)
        try:
            with canonical_write_transaction(self._engine) as connection:
                newly_observed = tuple(
                    reference.item_id
                    for reference, _ in items
                    if self._ensure_observed(connection, reference)
                )
                for reference, observation in items:
                    connection.execute(
                        queue_items.update()
                        .where(queue_items.c.item_id == reference.item_id.value)
                        .values(
                            observed_title=observation.title,
                            title_observed_at=observation.observed_at.value,
                            retired_at=None,
                        )
                    )
                return QueueItemsReconciled(
                    observed,
                    newly_observed,
                    self._retire_absent(connection, project, observed, observed_at),
                )
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    @staticmethod
    def _retire_absent(
        connection: Connection,
        project: ProjectId,
        observed: tuple[QueueItemId, ...],
        retired_at: RecordedAt,
    ) -> tuple[QueueItemId, ...]:
        absent = tuple(
            QueueItemId(str(value))
            for value in connection.scalars(
                sa.select(queue_items.c.item_id)
                .where(
                    queue_items.c.project_id == project.value,
                    queue_items.c.retired_at.is_(None),
                    queue_items.c.item_id.not_in(
                        [item_id.value for item_id in observed]
                    ),
                )
                .order_by(queue_items.c.item_id)
            )
        )
        if absent:
            connection.execute(
                queue_items.update()
                .where(queue_items.c.item_id.in_([item_id.value for item_id in absent]))
                .values(retired_at=retired_at.value)
            )
        return absent

    def plan(self, command: PlanQueueItem) -> PlanQueueItemResult:
        reference = command.item_reference
        try:
            with canonical_write_transaction(self._engine) as connection:
                self._ensure_observed(connection, reference)
                record = (
                    connection.execute(
                        sa.select(queue_items).where(
                            queue_items.c.item_id == reference.item_id.value
                        )
                    )
                    .mappings()
                    .one()
                )
                snapshot = _snapshot_from_record(connection, record)
                outcome = snapshot.plan(command)
                if not isinstance(outcome, QueueItemProposed):
                    return outcome
                refusal = self._proposal_refusal(connection, command)
                if refusal is not None:
                    return refusal
                revision = outcome.revision.value
                proposal = outcome.proposal
                connection.execute(
                    queue_proposal_revisions.insert().values(
                        proposal_row(reference, revision, proposal)
                    )
                )
                for prerequisite in proposal.prerequisite_item_ids:
                    connection.execute(
                        queue_dependency_edges.insert().values(
                            item_id=reference.item_id.value,
                            proposal_revision=revision,
                            project_id=reference.project.value,
                            prerequisite_item_id=prerequisite.value,
                        )
                    )
                updated = connection.execute(
                    queue_items.update()
                    .where(
                        queue_items.c.item_id == reference.item_id.value,
                        queue_items.c.state == QueueItemState.OBSERVED.value,
                        queue_items.c.state_version == command.expected_revision.value,
                    )
                    .values(
                        state=QueueItemState.PROPOSED.value,
                        state_version=revision,
                        current_proposal_revision=revision,
                    )
                )
                if updated.rowcount != 1:
                    raise DurableQueueAdmissionConflict(
                        "queue item proposal CAS changed no row"
                    )
                return outcome
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def confirm(self, command: ConfirmQueueProposal) -> ConfirmQueueProposalResult:
        reference = command.item_reference
        try:
            with canonical_write_transaction(self._engine) as connection:
                record = (
                    connection.execute(
                        sa.select(queue_items).where(
                            queue_items.c.item_id == reference.item_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return DurableStateCorrupt()
                snapshot = _snapshot_from_record(connection, record)
                outcome = snapshot.confirm(command)
                if isinstance(outcome, QueueItemAdmitted):
                    authority = outcome.admission.authority
                    if authority is None:
                        raise DurableQueueAdmissionConflict(
                            "a new queue admission has no decision authority"
                        )
                    updated = connection.execute(
                        queue_items.update()
                        .where(
                            queue_items.c.item_id == reference.item_id.value,
                            queue_items.c.state == QueueItemState.PROPOSED.value,
                            queue_items.c.state_version
                            == command.expected_revision.value,
                        )
                        .values(
                            state=QueueItemState.ADMITTED.value,
                            state_version=outcome.revision.value,
                            workflow_lineage_id=(
                                outcome.admission.workflow_lineage_id.value
                            ),
                            admission_rationale=outcome.admission.rationale.value,
                            decision_authority=authority.value,
                        )
                    )
                    if updated.rowcount != 1:
                        raise DurableQueueAdmissionConflict(
                            "queue item admission CAS changed no row"
                        )
                return outcome
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def current_policy(self, project: ProjectId) -> ReadQueueProjectPolicyResult:
        """The project's newest policy revision, or its absence, for a reader."""

        try:
            with self._engine.connect() as connection:
                policy = self._current_policy(connection, project)
        except (OperationalError, PoolTimeoutError):
            return QueueReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
        if policy is None:
            return QueueProjectPolicyAbsent()
        return QueueProjectPolicyFound(policy)

    def put_policy(
        self, policy: QueueProjectPolicyRevision, expected_revision: int
    ) -> PutQueueProjectPolicyResult:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected queue policy revision must be nonnegative")
        try:
            with canonical_write_transaction(self._engine) as connection:
                current = (
                    connection.execute(
                        sa.select(queue_project_policy_revisions)
                        .where(
                            queue_project_policy_revisions.c.project_id
                            == policy.project_id.value
                        )
                        .order_by(
                            queue_project_policy_revisions.c.revision_number.desc()
                        )
                        .limit(1)
                    )
                    .mappings()
                    .one_or_none()
                )
                actual = 0 if current is None else int(current["revision_number"])
                if current is not None and policy_from_record(current) == policy:
                    return QueueProjectPolicyUnchanged(policy)
                if expected_revision != actual or policy.revision_number != actual + 1:
                    return QueueProjectPolicyRevisionConflict(expected_revision, actual)
                connection.execute(
                    queue_project_policy_revisions.insert().values(policy_row(policy))
                )
                return QueueProjectPolicyPublished(policy)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def reserve_launch(self, binding: QueueLaunchBinding) -> ReserveQueueLaunchResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                existing = (
                    connection.execute(
                        sa.select(queue_launch_bindings).where(
                            queue_launch_bindings.c.item_id == binding.item_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    return QueueLaunchAlreadyBound(_binding_from_record(existing))
                record = (
                    connection.execute(
                        sa.select(queue_items).where(
                            queue_items.c.item_id == binding.item_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return DurableStateCorrupt()
                snapshot = _snapshot_from_record(connection, record)
                blockers = list(snapshot.blockers)
                policy = self._current_policy(
                    connection, snapshot.item_reference.project
                )
                if (
                    policy is not None
                    and self._active_launch_count(
                        connection, snapshot.item_reference.project
                    )
                    >= policy.maximum_active_runs
                ):
                    blockers.append(QueueBlockerKind.CAP_REACHED)
                if (
                    snapshot.state is not QueueItemState.ADMITTED
                    or snapshot.proposal is None
                    or snapshot.admission is None
                    or snapshot.admission.proposal_revision != binding.proposal_revision
                    or blockers
                ):
                    return QueueLaunchBlocked(
                        QueueItemSnapshot(
                            snapshot.item_reference,
                            snapshot.state,
                            snapshot.revision,
                            snapshot.admission,
                            snapshot.proposal,
                            snapshot.launch_binding,
                            tuple(dict.fromkeys(blockers)),
                            snapshot.observation,
                            snapshot.retired_at,
                        )
                    )
                connection.execute(
                    queue_launch_bindings.insert().values(
                        item_id=binding.item_id.value,
                        proposal_revision=binding.proposal_revision.value,
                        project_id=snapshot.item_reference.project.value,
                        run_id=binding.run_id.value,
                        workflow_revision_hash=binding.workflow_revision_hash.value,
                    )
                )
                return QueueLaunchReserved(binding)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def list_items(
        self, after: QueueItemId | None, limit: int
    ) -> QueueItemsPage | QueueReadUnavailable | DurableStateCorrupt:
        page = self._page_in_state(None, after, limit)
        if isinstance(page, QueueReadUnavailable | DurableStateCorrupt):
            return page
        return QueueItemsPage(*page)

    @staticmethod
    def _ensure_observed(connection: Connection, reference: WorkItemReference) -> bool:
        """Give the reference its OBSERVED row if it has none, saying whether it did."""

        inserted = connection.execute(
            sa.insert(queue_items)
            .prefix_with("OR IGNORE")
            .values(
                item_id=reference.item_id.value,
                project_id=reference.project.value,
                tracker_item_reference=reference.tracker_item.value,
                state=QueueItemState.OBSERVED.value,
                state_version=QUEUE_PROJECTION_REVISION_OBSERVED.value,
                workflow_lineage_id=None,
                admission_rationale=None,
                current_proposal_revision=None,
                decision_authority=None,
            )
        )
        stored_reference = connection.execute(
            sa.select(
                queue_items.c.project_id,
                queue_items.c.tracker_item_reference,
            ).where(queue_items.c.item_id == reference.item_id.value)
        ).one_or_none()
        if stored_reference != (reference.project.value, reference.tracker_item.value):
            raise ValueError("queue item id collides with a different work item")
        return inserted.rowcount == 1

    @staticmethod
    def _proposal_refusal(
        connection: Connection, command: PlanQueueItem
    ) -> QueueProposalRefused | None:
        proposal = command.proposal
        if command.item_reference.item_id in proposal.prerequisite_item_ids:
            return QueueProposalRefused(QueueProposalRefusal.SELF_DEPENDENCY)
        if proposal.policy_revision is not None:
            policy_exists = connection.scalar(
                sa.select(sa.literal(True)).where(
                    sa.exists(
                        sa.select(queue_project_policy_revisions.c.project_id).where(
                            queue_project_policy_revisions.c.project_id
                            == command.item_reference.project.value,
                            queue_project_policy_revisions.c.revision_number
                            == proposal.policy_revision,
                        )
                    )
                )
            )
            if policy_exists is not True:
                return QueueProposalRefused(
                    QueueProposalRefusal.POLICY_REVISION_MISSING
                )
        lineage_exists = connection.scalar(
            sa.select(sa.literal(True)).where(
                sa.exists(
                    sa.select(catalog_lineages.c.lineage_id).where(
                        catalog_lineages.c.lineage_id
                        == proposal.workflow_lineage_id.value
                    )
                )
            )
        )
        if lineage_exists is not True:
            return QueueProposalRefused(QueueProposalRefusal.WORKFLOW_LINEAGE_MISSING)
        if proposal.prerequisite_item_ids:
            rows = connection.execute(
                sa.select(queue_items.c.item_id).where(
                    queue_items.c.project_id == command.item_reference.project.value,
                    queue_items.c.item_id.in_(
                        tuple(item.value for item in proposal.prerequisite_item_ids)
                    ),
                )
            ).scalars()
            if set(rows) != {item.value for item in proposal.prerequisite_item_ids}:
                return QueueProposalRefused(
                    QueueProposalRefusal.PREREQUISITE_NOT_IN_PROJECT
                )
        if _closes_a_dependency_cycle(connection, command):
            return QueueProposalRefused(QueueProposalRefusal.DEPENDENCY_CYCLE)
        return None

    @staticmethod
    def _current_policy(
        connection: Connection, project: ProjectId
    ) -> QueueProjectPolicyRevision | None:
        record = (
            connection.execute(
                sa.select(queue_project_policy_revisions)
                .where(queue_project_policy_revisions.c.project_id == project.value)
                .order_by(queue_project_policy_revisions.c.revision_number.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return None if record is None else policy_from_record(record)

    @staticmethod
    def _active_launch_count(connection: Connection, project: ProjectId) -> int:
        count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(
                queue_launch_bindings.outerjoin(
                    runs, queue_launch_bindings.c.run_id == runs.c.run_id
                )
            )
            .where(
                queue_launch_bindings.c.project_id == project.value,
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

    def _page_in_state(
        self, state: QueueItemState | None, after: QueueItemId | None, limit: int
    ) -> (
        tuple[tuple[QueueItemSnapshot, ...], QueueItemId | None]
        | QueueReadUnavailable
        | DurableStateCorrupt
    ):
        if (
            type(limit) is not int
            or not MINIMUM_PAGE_ITEMS <= limit <= MAXIMUM_PAGE_ITEMS
        ):
            raise ValueError(
                f"queue item page limit must be an integer from {MINIMUM_PAGE_ITEMS} to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._engine.connect() as connection:
                statement = sa.select(queue_items).select_from(_QUEUE_START_ORDER_JOIN)
                if state is not None:
                    statement = statement.where(queue_items.c.state == state.value)
                if after is not None:
                    after_key = self._queue_start_order_key(connection, after)
                    statement = statement.where(
                        sa.tuple_(*_QUEUE_START_ORDER_COLUMNS)
                        > sa.tuple_(*(sa.literal(member) for member in after_key))
                    )
                records = (
                    connection.execute(
                        statement.order_by(*_QUEUE_START_ORDER_COLUMNS).limit(limit + 1)
                    )
                    .mappings()
                    .all()
                )
                has_more = len(records) > limit
                page_records = records[:limit]
                items = tuple(
                    _snapshot_from_record(connection, record) for record in page_records
                )
                next_after = (
                    QueueItemId(str(page_records[-1]["item_id"]))
                    if has_more and page_records
                    else None
                )
                return items, next_after
        except (OperationalError, PoolTimeoutError):
            return QueueReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    @staticmethod
    def _queue_start_order_key(
        connection: Connection, after: QueueItemId
    ) -> tuple[bool, int, str]:
        """The after-item's *current* start-order key, read fresh for this page.

        The wire cursor is opaque and only ever comes back from a `next_after`
        this store issued, so the item it names exists; the seek continues
        behind this key triple rather than behind the bare `item_id`, which
        would skip or repeat items whenever hash order disagrees with rank
        order (#1051).

        The guarantee holds exactly while the after-item's key is unchanged
        between the page that served it and this one. It is not unchanged in
        general: `QueueItemSnapshot.plan` lets an OBSERVED item (no proposal,
        sorted last) gain a proposal and become PROPOSED (ranked, sorted by
        `priority.rank`) at any time, which moves that item's key earlier.
        Once a proposal is planned it cannot be re-planned or withdrawn (`plan`
        refuses a second call for a PROPOSED or ADMITTED item), so this is the
        only key change this codebase can produce, and it only ever moves an
        item earlier. Read fresh here, an after-item that moved earlier since
        its page seeks from its new, earlier position: an item that used to
        sort between the old and new position is served again on the next
        page (a repeat), never dropped (a skip) -- there is no production path
        that moves an item later.
        """

        row = connection.execute(
            sa.select(*_QUEUE_START_ORDER_COLUMNS)
            .select_from(_QUEUE_START_ORDER_JOIN)
            .where(queue_items.c.item_id == after.value)
        ).one_or_none()
        if row is None:
            raise ValueError("the queue cursor names an item outside the projection")
        unranked, rank, item_id = row
        return bool(unranked), int(rank), str(item_id)


def _closes_a_dependency_cycle(connection: Connection, command: PlanQueueItem) -> bool:
    """Whether this proposal's prerequisites would close a cycle in the project.

    The graph is the project's current proposals plus the edges this command
    would add, so an item waiting on itself through any chain is refused before
    the proposal is written rather than found when nothing can ever start.
    """

    edges = {
        (str(item), str(prerequisite))
        for item, prerequisite in connection.execute(
            sa.select(
                queue_dependency_edges.c.item_id,
                queue_dependency_edges.c.prerequisite_item_id,
            )
            .join(
                queue_items,
                sa.and_(
                    queue_items.c.item_id == queue_dependency_edges.c.item_id,
                    queue_items.c.current_proposal_revision
                    == queue_dependency_edges.c.proposal_revision,
                ),
            )
            .where(
                queue_items.c.project_id == command.item_reference.project.value,
                queue_dependency_edges.c.project_id
                == command.item_reference.project.value,
            )
        )
    }
    edges.update(
        (command.item_reference.item_id.value, prerequisite.value)
        for prerequisite in command.proposal.prerequisite_item_ids
    )
    graph: dict[str, set[str]] = {}
    for item, prerequisite in edges:
        graph.setdefault(item, set()).add(prerequisite)
        graph.setdefault(prerequisite, set())
    visiting: set[str] = set()
    visited: set[str] = set()

    def reaches_itself(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(reaches_itself(prerequisite) for prerequisite in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(reaches_itself(node) for node in tuple(graph))


def _binding_from_record(record: Mapping[Any, Any]) -> QueueLaunchBinding:
    return QueueLaunchBinding(
        QueueItemId(str(record["item_id"])),
        QueueProjectionRevision(int(record["proposal_revision"])),
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["workflow_revision_hash"])),
    )
