"""The queue sweep: admit what the project's automation label names, then start.

Both halves run on the same trigger and read the same projection, so they live
together: `admit_queue_items_by_label` turns the operator's label in the
tracker into the proposal the project's policy defaults name and the one
durable admission decision an automation rule may make, and `advance_queue`
starts each exact launch of an admitted item once. The cap and the priority
govern the start, never the admission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, assert_never

from atelier2.application.admit_queue_item import confirm_queue_proposal
from atelier2.application.plan_queue_item import plan_queue_item
from atelier2.application.queue_sweep_reads import (
    QueueAdvanceCorrupt,
    QueueAdvanceUnavailable,
    RestartAuthority,
    active_policy,
    open_tracker_items,
    projected_items,
    validated_snapshot,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.application.start_published_run import (
    AgentConfigurationRevisionMissing,
    AgentExecutorBindingUnavailable,
    AuthoredOrder,
    BindingConstraintRefused,
    InvalidAgentBindings,
    RevisionMissing,
    RunCreated,
    RunExisting,
    RunFormatNotExecutable,
    RunIdentityConflict,
    RunInputRefused,
    UncastAgentRoles,
    WorkItemOrderUnreadable,
    start_published_run,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.orders import WorkItemOrderValue
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_LAUNCH_RESTARTS,
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionOutcome,
    QueueAdmissionRationale,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemProposed,
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalAlreadyCurrent,
    QueueProposalSource,
    QueueRestartRefusal,
    ReleaseQueueLaunch,
    queue_start_order_key,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import (
    UNSUCCESSFUL_TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.work_items import WORK_ITEM_ORDER_SCHEMA_REVISION
from atelier2.contracts.workflow_refusals import WorkflowDocumentInvalid
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    DurableWriteUnavailable,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.issue_observation import (
    TrackerItemSource,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
)
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogNameMissing,
    CatalogResolver,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueLaunchAlreadyBound,
    QueueLaunchBlocked,
    QueueLaunchReleased,
    QueueLaunchReleaseRefused,
    QueueLaunchReserved,
    QueueLaunchRestartsExhausted,
    QueueLaunchRunEnded,
    QueueLaunchRunOpen,
    QueueLaunchRuns,
    QueueProjection,
    QueueReadUnavailable,
)
from atelier2.ports.workflow_revisions import WorkflowDocumentParser

_LOG = logging.getLogger("atelier2")

_QUEUE_ITEM_RUN_DOMAIN = "queue-item-run/v2"
# The durable reason an automatic admission records, followed by the label that
# authorized it: the record says which rule admitted the item, not merely that
# some rule did.
_AUTOMATION_ADMISSION_REASON: Final = "the tracker item carries the automation label "


@dataclass(frozen=True)
class QueueRunStarted:
    item_id: QueueItemId
    binding: QueueLaunchBinding
    run: AnyRun


@dataclass(frozen=True)
class QueueRunAlreadyActive:
    item_id: QueueItemId
    binding: QueueLaunchBinding
    run: AnyRun


@dataclass(frozen=True)
class QueueItemBlocked:
    item_id: QueueItemId
    blockers: tuple[QueueBlockerKind, ...]


@dataclass(frozen=True)
class QueueItemRestarting:
    """The item's ended run gave its binding back; the next sweep starts it anew."""

    item_id: QueueItemId
    binding: QueueLaunchBinding
    ended_state: RunState
    restarts_spent: int


@dataclass(frozen=True)
class QueueItemRestartsExhausted:
    """The item keeps its ended run: it has spent every restart it may have."""

    item_id: QueueItemId
    binding: QueueLaunchBinding
    ended_state: RunState
    restarts_spent: int


@dataclass(frozen=True)
class QueueItemRestartWithheld:
    """The item keeps its ended run: the tracker no longer authorizes a restart.

    Nothing durable changes -- the binding stays exactly as it ended -- so the
    item stays admitted and bound, as one past the cap does, and the next sweep
    asks the tracker again. Re-labelling an open item is what lets it restart.
    """

    item_id: QueueItemId
    binding: QueueLaunchBinding
    ended_state: RunState
    refusal: QueueRestartRefusal


type QueueAdvanceOutcome = (
    QueueRunStarted
    | QueueRunAlreadyActive
    | QueueItemBlocked
    | QueueItemRestarting
    | QueueItemRestartsExhausted
    | QueueItemRestartWithheld
)


@dataclass(frozen=True)
class QueueAutomationLabelUnset:
    """The project's policy names no label: no admission has an automation authority."""


@dataclass(frozen=True)
class QueueAutomationSourceUnreadable:
    """The tracker did not say which items carry the label, so none was admitted.

    Soft on purpose: the labels live outside this instance, and an unreachable
    tracker leaves every durable queue row exactly as it was. The next sweep
    asks again.
    """

    detail: str


@dataclass(frozen=True)
class QueueLabelAdmissionDeclined:
    """One labelled item the projection did not newly admit, in its own words."""

    item_id: QueueItemId
    outcome: QueueAdmissionOutcome


@dataclass(frozen=True)
class QueueLabelAdmissionsDecided:
    """What the rule decided about every labelled item of this project."""

    admitted: tuple[QueueItemId, ...]
    declined: tuple[QueueLabelAdmissionDeclined, ...]


type QueueLabelAdmissionOutcome = (
    QueueAutomationLabelUnset
    | QueueAutomationSourceUnreadable
    | QueueLabelAdmissionsDecided
)


@dataclass(frozen=True)
class _RequiredOrderUnavailable:
    """The document declares graph inputs this sweep has no material for."""


def admit_queue_items_by_label(
    queue: QueueProjection,
    *,
    project: ProjectId,
    tracker: TrackerItemSource,
    page_limit: int = MAXIMUM_PAGE_ITEMS,
) -> QueueLabelAdmissionOutcome:
    """Admit every item the project's automation label names, and no other.

    The label is the operator's own signal in the tracker (REQ-QUEUE-08): a
    human writes it there, the atelier never does, and this rule only decides
    whether an admission has an authority. It is read at the instant the rule
    decides, so an item whose label was removed before the sweep is not
    admitted by it.

    What the rule may admit is the projection's decision, not this function's:
    every labelled item goes through the same `confirm` CAS the operator's
    door uses, under `AUTOMATION_RULE`. An item reserved for a human and one
    already admitted are therefore declined by the contract itself and left
    exactly as they were. Admission is not a start: the cap and the priority
    still govern what `advance_queue` starts afterwards.

    A labelled item that carries no proposal is proposed first when the policy
    states its defaults, so the label alone is the operator's whole handgrip
    and what it writes is still only a proposal (REQ-QUEUE-01); without them
    the item stays observed and the admission says so, exactly as before.
    """

    policy = active_policy(queue, project)
    if policy is None or policy.automation_label is None:
        return QueueAutomationLabelUnset()
    label = policy.automation_label
    listing = open_tracker_items(tracker, project, label)
    if isinstance(listing, TrackerSourceUnavailable | TrackerPayloadMalformed):
        return QueueAutomationSourceUnreadable(listing.detail)
    rationale = QueueAdmissionRationale(_AUTOMATION_ADMISSION_REASON + label)
    admitted: list[QueueItemId] = []
    declined: list[QueueLabelAdmissionDeclined] = []
    for item in projected_items(queue, page_limit):
        item_id = item.item_reference.item_id
        # A retired item has left the pullable set (ADR 0016, 2026-09-01
        # amendment); admitting one would write a decision the sweep then
        # refuses to act on.
        if item.retired_at is not None or item_id not in listing.labelled:
            continue
        expected_revision = _proposed_from_policy_defaults(queue, item, policy)
        outcome = _confirmed_by_rule(queue, item, expected_revision, rationale)
        if isinstance(outcome, QueueItemAdmitted):
            admitted.append(item_id)
        else:
            declined.append(QueueLabelAdmissionDeclined(item_id, outcome))
    return QueueLabelAdmissionsDecided(tuple(admitted), tuple(declined))


def _proposed_from_policy_defaults(
    queue: QueueProjection,
    item: QueueItemSnapshot,
    policy: QueueProjectPolicyRevision,
) -> QueueProjectionRevision:
    """Fill a labelled item's missing proposal from the policy's own defaults.

    Answers the revision the admission must now confirm: the proposal's own,
    whether this call wrote it or a concurrent sweep already wrote exactly it,
    and the item's own revision otherwise. A policy without defaults, an item
    that already carries a decision, and a proposal the projection refuses all
    leave the item exactly as it was, and the admission that follows reports in
    its own words why it was not admitted.
    """

    defaults = policy.defaults
    if defaults is None or item.state is not QueueItemState.OBSERVED:
        return item.revision
    outcome = plan_queue_item(
        PlanQueueItem(
            item.item_reference,
            QueueProposal(
                defaults.priority,
                defaults.workflow_lineage_id,
                (),
                defaults.automation_disposition,
                policy.revision_number,
                QueueProposalSource.POLICY_DEFAULT,
            ),
            item.revision,
        ),
        queue,
    )
    if isinstance(outcome, WriteUnavailable):
        raise QueueAdvanceUnavailable("a proposal from the policy could not commit")
    if isinstance(outcome, DurableStateCorrupt):
        raise QueueAdvanceCorrupt("a proposal from the policy found corrupt state")
    if isinstance(outcome, QueueItemProposed | QueueProposalAlreadyCurrent):
        return outcome.revision
    return item.revision


def _confirmed_by_rule(
    queue: QueueProjection,
    item: QueueItemSnapshot,
    expected_revision: QueueProjectionRevision,
    rationale: QueueAdmissionRationale,
) -> QueueAdmissionOutcome:
    outcome = confirm_queue_proposal(
        ConfirmQueueProposal(
            item.item_reference,
            expected_revision,
            rationale,
            QueueDecisionAuthority.AUTOMATION_RULE,
        ),
        queue,
    )
    if isinstance(outcome, WriteUnavailable):
        raise QueueAdvanceUnavailable("an automatic admission could not commit")
    if isinstance(outcome, DurableStateCorrupt):
        raise QueueAdvanceCorrupt("an automatic admission found corrupt state")
    return outcome


def advance_queue(
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
    *,
    workflow_document_parser: WorkflowDocumentParser | None,
    served_project: ProjectId | None = None,
    tracker: TrackerItemSource | None = None,
    page_limit: int = MAXIMUM_PAGE_ITEMS,
) -> tuple[QueueAdvanceOutcome, ...]:
    """Start each exact queue launch once, carrying the item it is about.

    `workflow_document_parser` is what turns a bound revision's published bytes
    into the graph a start can read `graph_inputs` from (ADR 0007's parsing
    stays an adapter concern, so this is handed in rather than imported).
    Passed `None`, a document is started exactly as before, bindings
    unexamined -- a caller says so explicitly rather than falling into it by
    omission; the live sweep always supplies the real parser.

    An admitted item naming a project other than `served_project` is not this
    instance's item -- a foreign `project_id` reaches here through
    `PUT /queue-proposals`, or the served project changes with old rows left
    behind -- so the sweep leaves it untouched (no launch binding, no run, no
    blocker invented) and continues with the next admitted item.
    """
    admitted_items = [
        item
        for item in projected_items(queue, page_limit)
        # A retired item has left the pullable set (ADR 0016, 2026-09-01
        # amendment): it stays visible in the projection, but the pull never
        # starts it again.
        if item.retired_at is None and item.state is QueueItemState.ADMITTED
    ]
    ordered = sorted(admitted_items, key=queue_start_order_key)
    restart_authority = RestartAuthority(queue, served_project, tracker)
    outcomes = (
        _released_or_advanced(
            item,
            queue,
            catalog,
            starter,
            workflow_document_parser,
            served_project,
            tracker,
            restart_authority,
        )
        for item in ordered
    )
    return tuple(outcome for outcome in outcomes if outcome is not None)


def _released_or_advanced(
    item: QueueItemSnapshot,
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
    workflow_document_parser: WorkflowDocumentParser | None,
    served_project: ProjectId | None,
    tracker: TrackerItemSource | None,
    restart_authority: RestartAuthority,
) -> QueueAdvanceOutcome | None:
    """Give back an ended launch before deciding anything else about the item.

    A sweep that releases an item does not also start it: the item rejoins the
    start order at the revision the release wrote, where the cap and the
    priority decide about it again like any other admitted item.
    """

    if served_project is not None and item.item_reference.project != served_project:
        return None
    released = _release_ended_launch(item, queue, restart_authority)
    if released is not None:
        return released
    return _advance_one(
        item, queue, catalog, starter, workflow_document_parser, served_project, tracker
    )


def _release_ended_launch(
    item: QueueItemSnapshot, queue: QueueLaunchRuns, authority: RestartAuthority
) -> QueueItemRestarting | QueueItemRestartsExhausted | QueueItemRestartWithheld | None:
    """Give back the binding of a run that ended without an answer.

    `None` for every launch this sweep treats exactly as it always did: an item
    with no binding, a run still going, a reservation whose start never landed,
    and a COMPLETED run -- that run is the item's answer, and a second one
    would spend money on a question already answered.

    The tracker is asked before the cap and before the release, so a launch
    the tracker no longer authorizes never reaches a write.
    """

    binding = item.launch_binding
    if binding is None:
        return None
    ended = _ended_launch(queue, binding)
    if ended is None or ended.state not in UNSUCCESSFUL_TERMINAL_RUN_STATES:
        return None
    item_id = binding.item_id
    refusal = authority.refusal_for(item_id)
    if refusal is not None:
        _LOG.info(
            "Queue item %s keeps its %s run: not restarted (%s).",
            item_id.value,
            ended.state.value,
            refusal.value,
            extra={
                "event": "queue_restart_withheld",
                "item_id": item_id.value,
                "refusal": refusal.value,
            },
        )
        return QueueItemRestartWithheld(item_id, binding, ended.state, refusal)
    # The release holds the cap itself; this answer only spares the transaction.
    if ended.restarts_spent >= MAXIMUM_QUEUE_LAUNCH_RESTARTS:
        return QueueItemRestartsExhausted(
            item_id, binding, ended.state, ended.restarts_spent
        )
    match queue.release_launch(ReleaseQueueLaunch(binding, ended.state)):
        case QueueLaunchReleased():
            return QueueItemRestarting(
                item_id, binding, ended.state, ended.restarts_spent + 1
            )
        case QueueLaunchRestartsExhausted(restarts_spent=spent):
            return QueueItemRestartsExhausted(item_id, binding, ended.state, spent)
        case QueueLaunchReleaseRefused():
            return None
        case DurableWriteUnavailable():
            raise QueueAdvanceUnavailable("the ended launch could not be released")
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt("the ended launch found corrupt state")
        case _ as unreachable:
            assert_never(unreachable)


def _ended_launch(
    queue: QueueLaunchRuns, binding: QueueLaunchBinding
) -> QueueLaunchRunEnded | None:
    match queue.read_launch(binding):
        case QueueLaunchRunEnded() as ended:
            return ended
        case QueueLaunchRunOpen():
            return None
        case QueueReadUnavailable():
            raise QueueAdvanceUnavailable("a bound run's state could not be read")
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt("a bound run contradicted its own contract")
        case _ as unreachable:
            assert_never(unreachable)


def _advance_one(
    item: QueueItemSnapshot,
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
    workflow_document_parser: WorkflowDocumentParser | None,
    served_project: ProjectId | None,
    tracker: TrackerItemSource | None,
) -> QueueAdvanceOutcome | None:
    binding = item.launch_binding
    if binding is None:
        proposal = item.proposal
        admission = item.admission
        if (
            item.state is QueueItemState.ADMITTED
            and proposal is None
            and admission is not None
            and admission.authority is None
            and admission.proposal_revision is None
        ):
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,),
            )
        if (
            item.state is not QueueItemState.ADMITTED
            or proposal is None
            or admission is None
            or admission.authority is None
            or admission.proposal_revision is None
        ):
            raise QueueAdvanceCorrupt(
                "the queue item does not carry one complete admitted proposal"
            )
        if item.blockers:
            return QueueItemBlocked(item.item_reference.item_id, item.blockers)
        revision_hash = _resolve_head(proposal.workflow_lineage_id, catalog)
        if revision_hash is None:
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.BINDING_UNRESOLVED,),
            )
        proposed_binding = QueueLaunchBinding(
            item.item_reference.item_id,
            admission.proposal_revision,
            _derive_run_id(
                item.item_reference.item_id, admission.proposal_revision.value
            ),
            revision_hash,
        )
        reservation = queue.reserve_launch(proposed_binding)
        match reservation:
            case QueueLaunchReserved(binding=reserved):
                binding = reserved
            case QueueLaunchAlreadyBound(binding=reserved):
                binding = reserved
            case QueueLaunchBlocked(item=blocked):
                blocked = validated_snapshot(blocked)
                return QueueItemBlocked(
                    blocked.item_reference.item_id, blocked.blockers
                )
            case DurableWriteUnavailable():
                raise QueueAdvanceUnavailable("the launch reservation could not commit")
            case PortDurableStateCorrupt():
                raise QueueAdvanceCorrupt("the launch reservation found corrupt state")
            case _:
                raise QueueAdvanceCorrupt(
                    "the queue answered an unknown launch reservation outcome"
                )
    order = _bound_work_item_order(item, binding, catalog, workflow_document_parser)
    if isinstance(order, _RequiredOrderUnavailable):
        # The document declares graph inputs this sweep cannot fill, so the
        # item is blocked without asking the starter (pinned by
        # `test_a_document_declaring_more_than_the_sweep_can_fill_is_blocked_not_guessed_at`).
        # A run that a fillable earlier read already started for this same
        # binding needs a durable read by run identity to recover -- residual,
        # not solved by re-asking the starter with an empty order (#1145).
        return QueueItemBlocked(
            item.item_reference.item_id,
            (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,),
        )
    if order is None:
        result = start_published_run(
            binding.run_id,
            binding.workflow_revision_hash,
            None,
            starter,
            project=served_project,
        )
    else:
        result = start_published_run(
            binding.run_id,
            binding.workflow_revision_hash,
            (),
            starter,
            orders=(order,),
            project=served_project,
            tracker=tracker,
        )
    match result:
        case RunCreated(run):
            return QueueRunStarted(item.item_reference.item_id, binding, run)
        case RunExisting(run):
            return QueueRunAlreadyActive(item.item_reference.item_id, binding, run)
        case InvalidAgentBindings() | UncastAgentRoles():
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.BINDING_UNRESOLVED,),
            )
        case WorkItemOrderUnreadable() | RunInputRefused():
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,),
            )
        case (
            RevisionMissing()
            | RunIdentityConflict()
            | RunFormatNotExecutable()
            | BindingConstraintRefused()
            | AgentConfigurationRevisionMissing()
            | AgentExecutorBindingUnavailable()
        ):
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.START_REFUSED,),
            )
        case WriteUnavailable():
            raise QueueAdvanceUnavailable("the reserved queue run could not start")
        case DurableStateCorrupt():
            raise QueueAdvanceCorrupt("the reserved queue run found corrupt state")
        case _:
            raise QueueAdvanceCorrupt("run start answered an unknown outcome")


def _resolve_head(
    lineage_id: CatalogLineageId, catalog: CatalogResolver
) -> WorkflowRevisionHash | None:
    match catalog.resolve_name(RevisionKind.WORKFLOW, lineage_id, "head"):
        case CatalogNameFound(revision_hash=revision_hash):
            return WorkflowRevisionHash(revision_hash.value)
        case CatalogNameMissing():
            return None
        case PublishedRevisionsUnavailable():
            raise QueueAdvanceUnavailable(
                f"the catalog could not resolve workflow lineage {lineage_id.value}"
            )
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt(
                f"workflow lineage {lineage_id.value} has corrupt catalog state"
            )
        case _:
            raise QueueAdvanceCorrupt("the catalog answered an unknown resolve outcome")


def _bound_work_item_order(
    item: QueueItemSnapshot,
    binding: QueueLaunchBinding,
    catalog: CatalogResolver,
    workflow_document_parser: WorkflowDocumentParser | None,
) -> AuthoredOrder | _RequiredOrderUnavailable | None:
    """The one order the bound document's `graph_inputs` asks this sweep to fill.

    `None` for a document with no `graph_inputs` (starts as today) or when no
    parser was handed in. A document that declares anything else -- more than
    one input, or one pinned to a schema other than the work-item order's --
    names material this sweep has no way to supply, so it is unfillable rather
    than guessed at.
    """

    if workflow_document_parser is None:
        return None
    document = _resolve_document(
        binding.workflow_revision_hash, catalog, workflow_document_parser
    )
    graph_inputs = document.graph_inputs
    if not graph_inputs:
        return None
    if len(graph_inputs) != 1:
        return _RequiredOrderUnavailable()
    (graph_input,) = graph_inputs
    if graph_input.schema_reference.revision != WORK_ITEM_ORDER_SCHEMA_REVISION.value:
        return _RequiredOrderUnavailable()
    return AuthoredOrder(
        graph_input.name, WorkItemOrderValue(item.item_reference.tracker_item)
    )


def _resolve_document(
    revision_hash: WorkflowRevisionHash,
    catalog: CatalogResolver,
    parser: WorkflowDocumentParser,
) -> AnyWorkflowDocument:
    match catalog.resolve(
        RevisionKind.WORKFLOW, PublishedRevisionHash(revision_hash.value)
    ):
        case PublishedRevisionFound(revision=revision):
            try:
                return parser(revision.document)
            except WorkflowDocumentInvalid as error:
                raise QueueAdvanceCorrupt(
                    f"workflow revision {revision_hash.value} is bound but not a "
                    "readable document"
                ) from error
        case PublishedRevisionMissing():
            raise QueueAdvanceCorrupt(
                f"workflow revision {revision_hash.value} is bound but unpublished"
            )
        case PublishedRevisionsUnavailable():
            raise QueueAdvanceUnavailable(
                f"the catalog could not resolve workflow revision {revision_hash.value}"
            )
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt(
                f"workflow revision {revision_hash.value} has corrupt catalog state"
            )
        case _:
            raise QueueAdvanceCorrupt("the catalog answered an unknown resolve outcome")


def _derive_run_id(item_id: QueueItemId, proposal_revision: int) -> RunId:
    return RunId(
        Sha256Hash.of(
            frame(
                _QUEUE_ITEM_RUN_DOMAIN,
                item_id.value.encode("ascii"),
                str(proposal_revision).encode("ascii"),
            )
        ).value
    )
