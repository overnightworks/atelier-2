"""The one typed queue projection and its policy/proposal/admission CAS doors."""

from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    decode_public_project_reference_value,
    require_json_media_dependency,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import (
    PROJECT_QUEUE_POLICY_PATH,
    PROJECT_SOURCE_IMPORT_PATH,
    QUEUE_ADMISSIONS_PATH,
    QUEUE_ITEMS_PATH,
    QUEUE_PROPOSALS_PATH,
)
from atelier2.api.problems import ApiProblem
from atelier2.api.references import (
    PageLimitQuery,
    PublicProjectReferencePath,
    QueueItemIdQuery,
)
from atelier2.api.wire.queue import (
    QueueAdmissionDecisionResource,
    QueueAdmissionResource,
    QueueItemPageResource,
    QueueItemResource,
    QueueLaunchBindingResource,
    QueuePriorityRankResource,
    QueueProjectPolicyResource,
    QueueProposalDecisionResource,
    QueueProposalResource,
)
from atelier2.api.wire.requests import (
    ConfirmQueueProposalRequestResource,
    PutQueueProjectPolicyRequestResource,
    PutQueueProposalRequestResource,
)
from atelier2.api.wire.resources import (
    InvalidFieldResource,
    ProjectSourceImportResource,
)
from atelier2.application.admit_queue_item import QueueItemsListed
from atelier2.application.import_project_source_issues import (
    ProjectSourceIssuesImported,
)
from atelier2.application.plan_queue_item import (
    QueueProjectPolicyPublished,
    QueueProjectPolicyRevisionConflict,
    QueueProjectPolicyUnchanged,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectSourceNotConnected,
    ReadUnavailable,
    SourcePayloadMalformed,
    WriteUnavailable,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.pages import DEFAULT_PAGE_LIMIT
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmission,
    QueueAdmissionAlreadyCurrent,
    QueueAdmissionAlreadyDecided,
    QueueAdmissionAuthorityRefused,
    QueueAdmissionProposalRequired,
    QueueAdmissionRationale,
    QueueAdmissionRevisionConflict,
    QueueAutomationDisposition,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemProposed,
    QueueItemSnapshot,
    QueueItemState,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyDefaults,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalAlreadyCurrent,
    QueueProposalAlreadyDecided,
    QueueProposalRefused,
    QueueProposalRevisionConflict,
    TrackerItemReference,
    WorkItemReference,
)

router = APIRouter()


@router.put(
    PROJECT_QUEUE_POLICY_PATH,
    response_model=QueueProjectPolicyResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": QueueProjectPolicyResource}},
)
async def put_queue_project_policy_route(
    public_project_reference: PublicProjectReferencePath,
    body: PutQueueProjectPolicyRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    project = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    try:
        policy = QueueProjectPolicyRevision(
            project,
            body.revision_number,
            body.maximum_active_runs,
            body.automation_label,
            _policy_defaults(body),
        )
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.put_queue_project_policy(
            policy, body.expected_revision
        ),
    )
    match result:
        case QueueProjectPolicyPublished(stored):
            status = HTTPStatus.CREATED
        case QueueProjectPolicyUnchanged(stored):
            status = HTTPStatus.OK
        case QueueProjectPolicyRevisionConflict():
            raise ApiProblem("queue-policy-revision-conflict")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(_policy_resource(stored), status)


@router.put(
    QUEUE_PROPOSALS_PATH,
    response_model=QueueProposalDecisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": QueueProposalDecisionResource}},
)
async def put_queue_proposal_route(
    body: PutQueueProposalRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    try:
        command = PlanQueueItem(
            _item_reference(body.project_id, body.tracker_item_reference),
            QueueProposal(
                QueuePriorityRank(body.priority.rank),
                CatalogLineageId(body.workflow_lineage_id),
                tuple(QueueItemId(value) for value in body.prerequisite_item_ids),
                QueueAutomationDisposition(body.automation_disposition),
                body.policy_revision,
            ),
            QueueProjectionRevision(body.expected_revision),
        )
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    result = await run_control_query(
        context.control_runner, lambda: context.use_cases.plan_queue_item(command)
    )
    match result:
        case QueueItemProposed(item_reference, proposal, revision):
            status = HTTPStatus.CREATED
        case QueueProposalAlreadyCurrent(item_reference, proposal, revision):
            status = HTTPStatus.OK
        case QueueProposalRevisionConflict():
            raise ApiProblem("queue-proposal-revision-conflict")
        case QueueProposalAlreadyDecided():
            raise ApiProblem("queue-proposal-already-decided")
        case QueueProposalRefused():
            raise ApiProblem("queue-proposal-refused")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        QueueProposalDecisionResource(
            item_id=item_reference.item_id.value,
            state=QueueItemState.PROPOSED,
            revision=revision.value,
            proposal=_proposal_resource(proposal, revision),
        ),
        status,
    )


@router.post(
    QUEUE_ADMISSIONS_PATH,
    response_model=QueueAdmissionDecisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": QueueAdmissionDecisionResource}},
)
async def confirm_queue_proposal_route(
    request: ConfirmQueueProposalRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    try:
        command = ConfirmQueueProposal(
            _item_reference(request.project_id, request.tracker_item_reference),
            QueueProjectionRevision(request.expected_revision),
            QueueAdmissionRationale(request.rationale),
        )
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.confirm_queue_proposal(command),
    )
    match result:
        case QueueItemAdmitted(item_reference, admission, revision):
            status = HTTPStatus.CREATED
        case QueueAdmissionAlreadyCurrent(item_reference, admission, revision):
            status = HTTPStatus.OK
        case QueueAdmissionRevisionConflict():
            raise ApiProblem("queue-admission-revision-conflict")
        case QueueAdmissionAlreadyDecided():
            raise ApiProblem("queue-admission-already-decided")
        case QueueAdmissionAuthorityRefused():
            raise ApiProblem("queue-admission-authority-refused")
        case QueueAdmissionProposalRequired():
            raise ApiProblem("queue-admission-proposal-required")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    if context.request_queue_sweep is not None:
        # Admission is not a start: the sweep decides, under the cap and the
        # priority, and it is asked here so that decision is not a tick away.
        context.request_queue_sweep()
    return resource_response(
        QueueAdmissionDecisionResource(
            item_id=item_reference.item_id.value,
            state=QueueItemState.ADMITTED,
            revision=revision.value,
            admission=_admission_resource(admission),
        ),
        status,
    )


@router.get(QUEUE_ITEMS_PATH)
async def list_queue_items_route(
    after: QueueItemIdQuery | None = None,
    limit: PageLimitQuery = DEFAULT_PAGE_LIMIT,
    context: ApiContext = api_context_dependency,
) -> QueueItemPageResource:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_queue_items(
            None if after is None else _parse_after(after), limit
        ),
    )
    match result:
        case QueueItemsListed(items, next_after):
            return QueueItemPageResource(
                items=tuple(_snapshot_resource(item) for item in items),
                next_after=None if next_after is None else next_after.value,
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.post(PROJECT_SOURCE_IMPORT_PATH)
async def import_project_source_issues_route(
    context: ApiContext = api_context_dependency,
) -> ProjectSourceImportResource:
    result = await run_control_query(
        context.control_runner, context.use_cases.import_project_source_issues
    )
    match result:
        case ProjectSourceIssuesImported(observed, newly_observed):
            return ProjectSourceImportResource(
                observed=observed, newly_observed=newly_observed
            )
        case ProjectSourceNotConnected():
            raise ApiProblem("project-source-not-connected")
        case SourcePayloadMalformed(detail):
            raise ApiProblem("project-source-payload-malformed", detail)
        case ReadUnavailable(detail):
            raise ApiProblem("project-source-unavailable", detail)
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def _item_reference(project: str, tracker: str) -> WorkItemReference:
    return WorkItemReference(ProjectId(project), TrackerItemReference(tracker))


def _parse_after(value: str) -> QueueItemId:
    try:
        return QueueItemId(value)
    except ValueError as error:
        raise ApiProblem(
            "invalid-request",
            invalid_fields=(
                InvalidFieldResource(
                    path="query/after",
                    reason="not a queue item id this list resumes from",
                ),
            ),
        ) from error


def _policy_defaults(
    body: PutQueueProjectPolicyRequestResource,
) -> QueueProjectPolicyDefaults | None:
    """What a labelled item with no proposal is proposed under, or nothing.

    Workflow and rank are stated together or not at all, and a disposition
    without them states nothing: each of those halves would leave the sweep
    inventing the rest of a proposal. Unstated, the disposition is whatever
    the contract reserves for a human.
    """

    lineage_id = body.default_workflow_lineage_id
    priority_rank = body.default_priority_rank
    disposition = body.automation_disposition_default
    if lineage_id is None and priority_rank is None:
        if disposition is not None:
            raise ValueError(
                "a queue automation disposition needs the defaults it applies to"
            )
        return None
    if lineage_id is None or priority_rank is None:
        raise ValueError("a queue policy default names a workflow and a priority")
    priority = QueuePriorityRank(priority_rank)
    lineage = CatalogLineageId(lineage_id)
    if disposition is None:
        return QueueProjectPolicyDefaults(lineage, priority)
    return QueueProjectPolicyDefaults(
        lineage, priority, QueueAutomationDisposition(disposition)
    )


def _policy_resource(policy: QueueProjectPolicyRevision) -> QueueProjectPolicyResource:
    defaults = policy.defaults
    return QueueProjectPolicyResource(
        project_id=policy.project_id.value,
        revision_number=policy.revision_number,
        maximum_active_runs=policy.maximum_active_runs,
        automation_label=policy.automation_label,
        default_workflow_lineage_id=(
            None if defaults is None else defaults.workflow_lineage_id.value
        ),
        default_priority_rank=None if defaults is None else defaults.priority.rank,
        automation_disposition_default=(
            None if defaults is None else defaults.automation_disposition
        ),
    )


def _proposal_resource(
    proposal: QueueProposal, revision: QueueProjectionRevision
) -> QueueProposalResource:
    return QueueProposalResource(
        revision=revision.value,
        priority=QueuePriorityRankResource(rank=proposal.priority.rank),
        workflow_lineage_id=proposal.workflow_lineage_id.value,
        prerequisite_item_ids=tuple(
            item_id.value for item_id in proposal.prerequisite_item_ids
        ),
        automation_disposition=proposal.automation_disposition,
        policy_revision=proposal.policy_revision,
        source=proposal.source,
    )


def _admission_resource(admission: QueueAdmission) -> QueueAdmissionResource:
    return QueueAdmissionResource(
        proposal_revision=(
            None
            if admission.proposal_revision is None
            else admission.proposal_revision.value
        ),
        authority=admission.authority,
        rationale=admission.rationale.value,
    )


def _snapshot_resource(snapshot: QueueItemSnapshot) -> QueueItemResource:
    proposal = snapshot.proposal
    admission = snapshot.admission
    binding = snapshot.launch_binding
    observation = snapshot.observation
    if proposal is None:
        legal_without_proposal = (
            snapshot.state is QueueItemState.OBSERVED
            and admission is None
            and binding is None
        ) or (
            snapshot.state is QueueItemState.ADMITTED
            and admission is not None
            and admission.proposal_revision is None
            and admission.authority is None
            and binding is None
        )
        if not legal_without_proposal:
            raise ApiProblem("durable-state-corrupt")
        proposal_revision = None
    elif snapshot.state is QueueItemState.PROPOSED:
        if admission is not None or binding is not None:
            raise ApiProblem("durable-state-corrupt")
        proposal_revision = snapshot.revision
    elif snapshot.state is QueueItemState.ADMITTED:
        if (
            admission is None
            or admission.proposal_revision is None
            or admission.authority is None
            or admission.workflow_lineage_id != proposal.workflow_lineage_id
            or snapshot.revision.value != admission.proposal_revision.value + 1
        ):
            raise ApiProblem("durable-state-corrupt")
        proposal_revision = admission.proposal_revision
    else:
        raise ApiProblem("durable-state-corrupt")
    return QueueItemResource(
        project_id=snapshot.item_reference.project.value,
        tracker_item_reference=snapshot.item_reference.tracker_item.value,
        item_id=snapshot.item_reference.item_id.value,
        state=snapshot.state,
        revision=snapshot.revision.value,
        proposal=(
            None
            if proposal is None or proposal_revision is None
            else _proposal_resource(proposal, proposal_revision)
        ),
        admission=None if admission is None else _admission_resource(admission),
        launch_binding=(
            None
            if binding is None
            else QueueLaunchBindingResource(
                proposal_revision=binding.proposal_revision.value,
                run_id=binding.run_id.value,
                workflow_revision_hash=binding.workflow_revision_hash.value,
            )
        ),
        blockers=snapshot.blockers,
        tracker_enrichment="ENRICHMENT_UNAVAILABLE",
        title=None if observation is None else observation.title,
        title_observed_at=None
        if observation is None
        else observation.observed_at.value,
        retired_at=None if snapshot.retired_at is None else snapshot.retired_at.value,
    )
