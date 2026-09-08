"""Typed durable decisions for one tracker-referenced queue item.

Tracker content stays behind its reference. Core owns the proposal an operator
inspects, the exact proposal admission confirms, and the immutable launch
binding that prevents a moving catalog head or a restart from spending an item
twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Final

from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.runs import (
    UNSUCCESSFUL_TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt

MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS = 1_024
MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS = 4_096
MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS = 256
MAXIMUM_QUEUE_ACTIVE_RUNS = 1_000
# How often one item may be given a fresh run after its own ended badly. Two
# restarts is three paid runs on the same item: enough that a flaky provider or
# a transient tool failure recovers without a person, and few enough that an
# item failing for its own reasons stops instead of spending a budget nobody is
# watching.
MAXIMUM_QUEUE_LAUNCH_RESTARTS: Final = 2
# GitHub's own issue and pull-request title bound; every tracker source this
# codebase reads titles from is GitHub today (ADR 0010).
MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS = 256
# A policy names exactly one label. "Admit everything" is a decision the
# operator has deliberately not made (#79 ruling 1, 04.09.2026), so the
# wildcard spelling is refused instead of being read as a literal label named
# `*` that no tracker item will ever carry.
QUEUE_AUTOMATION_LABEL_WILDCARD: Final = "*"


@dataclass(frozen=True)
class TrackerItemReference:
    """The opaque address of one item inside whichever tracker holds it.

    Core reads no more of the tracker than this reference carries: what the
    string means -- an issue number, a GitLab path -- is the connected
    platform adapter's contract (ADR 0010), never reinterpreted here.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a tracker item reference must be text")
        if not 1 <= len(self.value) <= MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS:
            raise ValueError(
                "a tracker item reference must contain 1 to "
                f"{MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS} characters"
            )


class QueueItemId(Sha256Hash):
    """The store-stable identity derived from one item's project and reference."""


@dataclass(frozen=True)
class QueueItemTrackerObservation:
    """The tracker title as it was last observed, and the one instant it was read.

    A dated memory of a tracker-owned fact, never core truth (ADR 0016,
    2026-09-01 amendment): a reader must not take `title` as what the item is
    called now, only as what the tracker said at `observed_at`. `observed_at`
    reuses the port's own reading clock (`OpenTrackerItemsObserved.observed_at`)
    rather than measuring a second time.
    """

    title: str
    observed_at: RecordedAt

    def __post_init__(self) -> None:
        if not isinstance(self.title, str):
            raise TypeError("a tracker title observation must carry text")
        if not 1 <= len(self.title) <= MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS:
            raise ValueError(
                "a tracker title observation must contain 1 to "
                f"{MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS} characters"
            )
        if not isinstance(self.observed_at, RecordedAt):
            raise TypeError(
                "a tracker title observation must carry its instant through RecordedAt"
            )


@dataclass(frozen=True)
class WorkItemReference:
    """Which connected project, and which item inside its tracker.

    The pair is the whole identity: two references naming the same project and
    the same tracker item resolve to the same queue row, by derivation rather
    than by an id a caller could hand in and have accepted.
    """

    project: ProjectId
    tracker_item: TrackerItemReference
    item_id: QueueItemId = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectId):
            raise TypeError(
                "a work item reference names its project through the contract"
            )
        if not isinstance(self.tracker_item, TrackerItemReference):
            raise TypeError(
                "a work item reference names its tracker item through the contract"
            )
        object.__setattr__(
            self,
            "item_id",
            QueueItemId.of(
                frame(
                    "queue-item/v1",
                    self.project.value.encode("utf-8"),
                    self.tracker_item.value.encode("utf-8"),
                )
            ),
        )


@dataclass(frozen=True)
class QueueProjectionRevision:
    """How many durable admission transitions one queue item has advanced."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or self.value < 0:
            raise ValueError(
                "QueueProjectionRevision must be a nonnegative advance count"
            )


QUEUE_PROJECTION_REVISION_OBSERVED: Final = QueueProjectionRevision(0)


class QueueItemState(StrEnum):
    """Proposal and admission are distinct durable decisions."""

    OBSERVED = "OBSERVED"
    PROPOSED = "PROPOSED"
    ADMITTED = "ADMITTED"


@dataclass(frozen=True, order=True)
class QueuePriorityRank:
    """A positive one-based queue rank; lower ranks run first."""

    rank: int

    def __post_init__(self) -> None:
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("a queue priority rank must be a positive integer")


class QueueDecisionAuthority(StrEnum):
    OPERATOR = "OPERATOR"
    AUTOMATION_RULE = "AUTOMATION_RULE"


class QueueAutomationDisposition(StrEnum):
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    AUTOMATION_AUTHORIZED = "AUTOMATION_AUTHORIZED"


class QueueProposalSource(StrEnum):
    """Which decision wrote this proposal: the operator's door, or the policy."""

    OPERATOR = "OPERATOR"
    POLICY_DEFAULT = "POLICY_DEFAULT"


class QueueProposalRefusal(StrEnum):
    SELF_DEPENDENCY = "SELF_DEPENDENCY"
    POLICY_REVISION_MISSING = "POLICY_REVISION_MISSING"
    WORKFLOW_LINEAGE_MISSING = "WORKFLOW_LINEAGE_MISSING"
    PREREQUISITE_NOT_IN_PROJECT = "PREREQUISITE_NOT_IN_PROJECT"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"


class QueueBlockerKind(StrEnum):
    PRIORITY_UNSET = "PRIORITY_UNSET"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    PREREQUISITE_OPEN = "PREREQUISITE_OPEN"
    PREREQUISITE_FAILED = "PREREQUISITE_FAILED"
    CAP_REACHED = "CAP_REACHED"
    BINDING_UNRESOLVED = "BINDING_UNRESOLVED"
    REQUIRED_ORDER_UNAVAILABLE = "REQUIRED_ORDER_UNAVAILABLE"
    START_REFUSED = "START_REFUSED"
    LEGACY_REVIEW_REQUIRED = "LEGACY_REVIEW_REQUIRED"


class QueueRestartRefusal(StrEnum):
    """Why the sweep left an ended launch bound instead of buying its item a run.

    A restart is an unattended, paid decision, so it needs the authority an
    automatic admission needs (REQ-QUEUE-08): the tracker, read at the instant
    the sweep decides, still lists the item open and carrying the automation
    label. Each member names which part of that authority was missing.
    """

    AUTOMATION_LABEL_UNSET = "AUTOMATION_LABEL_UNSET"
    TRACKER_UNREADABLE = "TRACKER_UNREADABLE"
    TRACKER_ITEM_CLOSED = "TRACKER_ITEM_CLOSED"
    LABEL_REMOVED = "LABEL_REMOVED"


@dataclass(frozen=True)
class QueueProjectPolicyDefaults:
    """What the label sweep proposes for an item the operator has only labelled.

    Workflow and priority travel together because a proposal carries both: a
    policy naming one of them would leave the sweep to guess the other. The
    disposition stays HUMAN_REQUIRED unless the operator states otherwise, so
    a default never releases work by itself (REQ-QUEUE-05).
    """

    workflow_lineage_id: CatalogLineageId
    priority: QueuePriorityRank
    automation_disposition: QueueAutomationDisposition = (
        QueueAutomationDisposition.HUMAN_REQUIRED
    )

    def __post_init__(self) -> None:
        if not isinstance(self.workflow_lineage_id, CatalogLineageId):
            raise TypeError(
                "a queue policy default names its workflow through the catalog "
                "lineage id"
            )
        if not isinstance(self.priority, QueuePriorityRank):
            raise TypeError(
                "a queue policy default priority must use QueuePriorityRank"
            )
        if not isinstance(self.automation_disposition, QueueAutomationDisposition):
            raise TypeError("a queue policy default disposition must be typed")


@dataclass(frozen=True)
class QueueProjectPolicyRevision:
    """One immutable project's automation filter and active-run ceiling."""

    project_id: ProjectId
    revision_number: int
    maximum_active_runs: int
    automation_label: str | None
    defaults: QueueProjectPolicyDefaults | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("a queue policy must name its project through ProjectId")
        if type(self.revision_number) is not int or self.revision_number < 1:
            raise ValueError("a queue policy revision number must be positive")
        if (
            type(self.maximum_active_runs) is not int
            or not 1 <= self.maximum_active_runs <= MAXIMUM_QUEUE_ACTIVE_RUNS
        ):
            raise ValueError(
                "a queue policy active-run cap must be between 1 and "
                f"{MAXIMUM_QUEUE_ACTIVE_RUNS}"
            )
        if self.automation_label is not None and (
            not isinstance(self.automation_label, str)
            or not 1
            <= len(self.automation_label)
            <= MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS
        ):
            raise ValueError(
                "a queue automation label must be absent or contain 1 to "
                f"{MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS} characters"
            )
        if self.automation_label == QUEUE_AUTOMATION_LABEL_WILDCARD:
            raise ValueError(
                "a queue automation label names one label; admitting every "
                "observed item is not a ruled policy value"
            )
        if self.defaults is not None and not isinstance(
            self.defaults, QueueProjectPolicyDefaults
        ):
            raise TypeError("a queue policy carries its defaults through the contract")


@dataclass(frozen=True)
class QueueProposal:
    """The exact triage decision an admission may later confirm."""

    priority: QueuePriorityRank
    workflow_lineage_id: CatalogLineageId
    prerequisite_item_ids: tuple[QueueItemId, ...]
    automation_disposition: QueueAutomationDisposition
    policy_revision: int | None = None
    source: QueueProposalSource = QueueProposalSource.OPERATOR

    def __post_init__(self) -> None:
        if not isinstance(self.priority, QueuePriorityRank):
            raise TypeError("a queue proposal priority must use QueuePriorityRank")
        if not isinstance(self.workflow_lineage_id, CatalogLineageId):
            raise TypeError("a queue proposal workflow must use CatalogLineageId")
        if not isinstance(self.prerequisite_item_ids, tuple) or any(
            not isinstance(item_id, QueueItemId)
            for item_id in self.prerequisite_item_ids
        ):
            raise TypeError("queue proposal prerequisites must be QueueItemId values")
        canonical = tuple(
            sorted(set(self.prerequisite_item_ids), key=lambda item: item.value)
        )
        object.__setattr__(self, "prerequisite_item_ids", canonical)
        if not isinstance(self.automation_disposition, QueueAutomationDisposition):
            raise TypeError("a queue proposal automation disposition must be typed")
        if self.policy_revision is not None and (
            type(self.policy_revision) is not int or self.policy_revision < 1
        ):
            raise ValueError("a proposal policy revision must be positive when present")
        if not isinstance(self.source, QueueProposalSource):
            raise TypeError("a queue proposal names the decision that wrote it")


@dataclass(frozen=True)
class QueueLaunchBinding:
    """The one immutable launch reservation an admitted item can ever receive."""

    item_id: QueueItemId
    proposal_revision: QueueProjectionRevision
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash

    def __post_init__(self) -> None:
        if self.proposal_revision.value < 1:
            raise ValueError("a launch binding must name a proposal revision")


@dataclass(frozen=True)
class ReleaseQueueLaunch:
    """Give one item its binding back because the run it named ended badly.

    The item stays admitted under the same proposal and rejoins the start order
    one proposal revision on, which is what makes its next launch a differently
    identified run rather than a second attempt at the same one.
    """

    binding: QueueLaunchBinding
    ended_state: RunState

    def __post_init__(self) -> None:
        if not isinstance(self.binding, QueueLaunchBinding):
            raise TypeError("a launch release names its binding through the contract")
        if self.ended_state not in UNSUCCESSFUL_TERMINAL_RUN_STATES:
            raise ValueError(
                "a completed or unfinished run keeps its binding; only a failed "
                "or cancelled one is given back"
            )


@dataclass(frozen=True)
class PlanQueueItem:
    item_reference: WorkItemReference
    proposal: QueueProposal
    expected_revision: QueueProjectionRevision


@dataclass(frozen=True)
class ConfirmQueueProposal:
    item_reference: WorkItemReference
    expected_revision: QueueProjectionRevision
    rationale: QueueAdmissionRationale
    authority: QueueDecisionAuthority = QueueDecisionAuthority.OPERATOR


@dataclass(frozen=True)
class QueueAdmissionRationale:
    """The durable reason recorded for one admission decision."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a queue admission rationale must be text")
        if not 1 <= len(self.value) <= MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS:
            raise ValueError(
                "a queue admission rationale must contain 1 to "
                f"{MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS} characters"
            )


@dataclass(frozen=True)
class QueueAdmission:
    """The one named workflow binding an admitted item carries, and why.

    The binding names a catalog lineage rather than a revision: which workflow
    this item runs under is a named decision that survives the lineage
    publishing later members, exactly as a catalog name resolves to `head`.
    """

    workflow_lineage_id: CatalogLineageId
    rationale: QueueAdmissionRationale
    authority: QueueDecisionAuthority | None = None
    proposal_revision: QueueProjectionRevision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workflow_lineage_id, CatalogLineageId):
            raise TypeError(
                "a queue admission names its workflow through the catalog lineage id"
            )
        if not isinstance(self.rationale, QueueAdmissionRationale):
            raise TypeError(
                "a queue admission carries its rationale through the contract"
            )
        if self.proposal_revision is None:
            if self.authority is not None:
                raise ValueError("a legacy admission cannot invent decision authority")
        else:
            if not isinstance(self.authority, QueueDecisionAuthority):
                raise TypeError(
                    "a proposed admission authority must use its typed contract"
                )
            if self.proposal_revision.value < 1:
                raise ValueError("an admission proposal revision must be positive")


@dataclass(frozen=True)
class QueueItemAdmitted:
    """The item advanced from OBSERVED to ADMITTED under this admission."""

    item_reference: WorkItemReference
    admission: QueueAdmission
    revision: QueueProjectionRevision


@dataclass(frozen=True)
class QueueAdmissionAlreadyCurrent:
    """A repeated request for exactly the admission already recorded: no mutation."""

    item_reference: WorkItemReference
    admission: QueueAdmission
    revision: QueueProjectionRevision


@dataclass(frozen=True)
class QueueAdmissionRevisionConflict:
    """The caller inspected a revision this item has since moved past."""

    expected: QueueProjectionRevision
    actual: QueueProjectionRevision


@dataclass(frozen=True)
class QueueAdmissionAlreadyDecided:
    """The item is already admitted under a different workflow binding or reason."""

    item_reference: WorkItemReference


@dataclass(frozen=True)
class QueueAdmissionAuthorityRefused:
    """An automation rule cannot confirm a proposal reserved for a human."""

    authority: QueueDecisionAuthority
    disposition: QueueAutomationDisposition


@dataclass(frozen=True)
class QueueAdmissionProposalRequired:
    """Confirmation cannot skip the proposal decision."""

    item_reference: WorkItemReference
    state: QueueItemState


type QueueAdmissionOutcome = (
    QueueItemAdmitted
    | QueueAdmissionAlreadyCurrent
    | QueueAdmissionRevisionConflict
    | QueueAdmissionAlreadyDecided
    | QueueAdmissionAuthorityRefused
    | QueueAdmissionProposalRequired
)


@dataclass(frozen=True)
class QueueItemProposed:
    item_reference: WorkItemReference
    proposal: QueueProposal
    revision: QueueProjectionRevision


@dataclass(frozen=True)
class QueueProposalAlreadyCurrent:
    item_reference: WorkItemReference
    proposal: QueueProposal
    revision: QueueProjectionRevision


@dataclass(frozen=True)
class QueueProposalRevisionConflict:
    expected: QueueProjectionRevision
    actual: QueueProjectionRevision


@dataclass(frozen=True)
class QueueProposalAlreadyDecided:
    item_reference: WorkItemReference
    state: QueueItemState


@dataclass(frozen=True)
class QueueProposalRefused:
    refusal: QueueProposalRefusal


type QueueProposalOutcome = (
    QueueItemProposed
    | QueueProposalAlreadyCurrent
    | QueueProposalRevisionConflict
    | QueueProposalAlreadyDecided
    | QueueProposalRefused
)


class QueueItemReferenceMismatch(RuntimeError):
    """A command named a different item than the snapshot it was resolved against."""


def _is_legacy_admission(snapshot: QueueItemSnapshot) -> bool:
    return (
        snapshot.state is QueueItemState.ADMITTED
        and snapshot.admission is not None
        and snapshot.proposal is None
    )


def _require_typed_snapshot_fields(snapshot: QueueItemSnapshot) -> None:
    if snapshot.observation is not None and not isinstance(
        snapshot.observation, QueueItemTrackerObservation
    ):
        raise TypeError(
            "a queue item snapshot carries its observation through the contract"
        )
    if snapshot.retired_at is not None and not isinstance(
        snapshot.retired_at, RecordedAt
    ):
        raise TypeError("a queue item snapshot carries retired_at as RecordedAt")


def _require_canonical_revision_for_state(snapshot: QueueItemSnapshot) -> None:
    if (
        snapshot.state is QueueItemState.OBSERVED
        and snapshot.revision != QUEUE_PROJECTION_REVISION_OBSERVED
    ):
        raise ValueError("an observed queue item must be at revision zero")
    if snapshot.state is QueueItemState.PROPOSED and snapshot.revision.value < 1:
        raise ValueError("a proposed queue item must have a positive revision")


def _require_admission_presence_agrees_with_state(snapshot: QueueItemSnapshot) -> None:
    if (snapshot.state is QueueItemState.ADMITTED) != (snapshot.admission is not None):
        raise ValueError(
            "a queue item snapshot carries an admission if and only if it is ADMITTED"
        )


def _require_proposal_presence_agrees_with_lifecycle(
    snapshot: QueueItemSnapshot,
) -> None:
    proposed = snapshot.state in {QueueItemState.PROPOSED, QueueItemState.ADMITTED}
    if proposed != (snapshot.proposal is not None) and not _is_legacy_admission(
        snapshot
    ):
        raise ValueError(
            "a proposed queue lifecycle carries the proposal it is based on"
        )


def _require_canonical_admission_shape(snapshot: QueueItemSnapshot) -> None:
    admission = snapshot.admission
    if _is_legacy_admission(snapshot):
        if (
            admission is None
            or admission.authority is not None
            or admission.proposal_revision is not None
        ):
            raise ValueError("only a proposal-less admission may be legacy-shaped")
    elif snapshot.state is QueueItemState.ADMITTED:
        proposal = snapshot.proposal
        if (
            admission is None
            or proposal is None
            or admission.authority is None
            or admission.proposal_revision is None
            or admission.workflow_lineage_id != proposal.workflow_lineage_id
            or snapshot.revision.value != admission.proposal_revision.value + 1
        ):
            raise ValueError(
                "an admitted proposal must name its exact authority and revision"
            )


def _require_canonical_launch_binding(snapshot: QueueItemSnapshot) -> None:
    if snapshot.launch_binding is None:
        return
    admission = snapshot.admission
    if (
        snapshot.state is not QueueItemState.ADMITTED
        or snapshot.proposal is None
        or admission is None
    ):
        raise ValueError("only a proposed admission can carry a launch binding")
    if snapshot.launch_binding.item_id != snapshot.item_reference.item_id:
        raise ValueError("a launch binding must name its queue item")
    if snapshot.launch_binding.proposal_revision != admission.proposal_revision:
        raise ValueError("a launch binding must name the admitted proposal")


@dataclass(frozen=True)
class QueueItemSnapshot:
    """One durable point-in-time view of a queue item's admission lifecycle."""

    item_reference: WorkItemReference
    state: QueueItemState
    revision: QueueProjectionRevision
    admission: QueueAdmission | None
    proposal: QueueProposal | None = None
    launch_binding: QueueLaunchBinding | None = None
    blockers: tuple[QueueBlockerKind, ...] = ()
    observation: QueueItemTrackerObservation | None = None
    retired_at: RecordedAt | None = None

    def __post_init__(self) -> None:
        _require_typed_snapshot_fields(self)
        _require_canonical_revision_for_state(self)
        _require_admission_presence_agrees_with_state(self)
        _require_proposal_presence_agrees_with_lifecycle(self)
        _require_canonical_admission_shape(self)
        _require_canonical_launch_binding(self)

    def revalidated(self) -> QueueItemSnapshot:
        """This snapshot built again through its own constructor, every field kept.

        `fields()` names every field the class declares -- including one a
        later change adds -- rather than a fixed positional list that would
        carry on quietly forgetting it, so the constructor's validation runs
        over exactly what this instance holds.
        """

        return QueueItemSnapshot(
            **{member.name: getattr(self, member.name) for member in fields(self)}
        )

    def plan(self, command: PlanQueueItem) -> QueueProposalOutcome:
        if command.item_reference != self.item_reference:
            raise QueueItemReferenceMismatch(
                "a proposal command must name the item its snapshot was resolved for"
            )
        if self.state is QueueItemState.PROPOSED:
            if self.proposal == command.proposal:
                return QueueProposalAlreadyCurrent(
                    self.item_reference, command.proposal, self.revision
                )
            return QueueProposalAlreadyDecided(self.item_reference, self.state)
        if self.state is QueueItemState.ADMITTED:
            return QueueProposalAlreadyDecided(self.item_reference, self.state)
        if command.expected_revision != self.revision:
            return QueueProposalRevisionConflict(
                command.expected_revision, self.revision
            )
        return QueueItemProposed(
            self.item_reference,
            command.proposal,
            QueueProjectionRevision(self.revision.value + 1),
        )

    def confirm(self, command: ConfirmQueueProposal) -> QueueAdmissionOutcome:
        if command.item_reference != self.item_reference:
            raise QueueItemReferenceMismatch(
                "an admission command must name the item its snapshot was resolved for"
            )
        if self.state is QueueItemState.ADMITTED:
            current = self.admission
            if current is None:
                raise QueueItemReferenceMismatch(
                    "an ADMITTED snapshot must carry its admission"
                )
            if (
                current.proposal_revision == command.expected_revision
                and current.rationale == command.rationale
                and current.authority is command.authority
            ):
                return QueueAdmissionAlreadyCurrent(
                    self.item_reference, current, self.revision
                )
            return QueueAdmissionAlreadyDecided(self.item_reference)
        if self.state is not QueueItemState.PROPOSED or self.proposal is None:
            return QueueAdmissionProposalRequired(
                self.item_reference,
                self.state,
            )
        if command.expected_revision != self.revision:
            return QueueAdmissionRevisionConflict(
                command.expected_revision, self.revision
            )
        if (
            command.authority is QueueDecisionAuthority.AUTOMATION_RULE
            and self.proposal.automation_disposition
            is not QueueAutomationDisposition.AUTOMATION_AUTHORIZED
        ):
            return QueueAdmissionAuthorityRefused(
                command.authority,
                self.proposal.automation_disposition,
            )
        admission = QueueAdmission(
            self.proposal.workflow_lineage_id,
            command.rationale,
            command.authority,
            self.revision,
        )
        return QueueItemAdmitted(
            self.item_reference,
            admission,
            QueueProjectionRevision(self.revision.value + 1),
        )


type QueueStartOrderKey = tuple[bool, int, str]


def queue_start_order_key(snapshot: QueueItemSnapshot) -> QueueStartOrderKey:
    """The one ordering an admitted item's start, and its list position, share.

    `advance_queue` starts admitted items in this order; `GET /queue-items`
    (the DBOS store) lists every item in this same order over the same three
    members, computed in SQL. One owner for the rule keeps the list an
    operator reads honest about the order a run would actually take: an item
    with no proposal (a legacy admission) sorts after every ranked item,
    lower `priority.rank` runs first, and `item_id` breaks a tie.
    """

    return (
        snapshot.proposal is None,
        snapshot.proposal.priority.rank if snapshot.proposal is not None else 0,
        snapshot.item_reference.item_id.value,
    )
