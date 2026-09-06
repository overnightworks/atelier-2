from __future__ import annotations

from http import HTTPStatus
from typing import NoReturn, assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    decode_base64,
    decode_public_reference,
    load_run_resource,
    require_field,
    require_fields,
    require_json_media_dependency,
    require_new_run_identity,
    require_run_projections,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.limits import ApiLimitExceeded
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import (
    ApiProblem,
    bounded_invalid_field,
)
from atelier2.api.projection.runs import (
    node_detail_resource,
    run_list_row_resource,
)
from atelier2.api.references import (
    DEFAULT_PAGE_LIMIT,
    AgentAttemptIdPathParameter,
    PageLimitParameter,
    PublicRunReferencePathParameter,
    PublicRunReferenceQueryParameter,
    encode_public_run_reference,
    parse_revision_hash,
)
from atelier2.api.wire.requests import (
    AnswerWaitRequestResource,
    AnyStartRunOrderResource,
    AnyStartRunRequestResource,
    ArtifactOrderResource,
    CancelAgentAttemptRequestResource,
    CancelRunRequestResource,
    ForkRunRequestResource,
    InlineOrderResource,
    ReconcileRunRequestResource,
    StartRunRequestResourceV2,
    StartRunRequestResourceV3,
    WorkItemOrderResource,
)
from atelier2.api.wire.resources import (
    InvalidFieldResource,
    NodeDetailResource,
    OperatorFoundDeterminationResource,
    RunResourceV3,
    UncastRoleResource,
    VersionedRunPageResource,
)
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerActorMismatch,
    AnswerExistingApplied,
    AnswerExistingPending,
    AnswerRevisionConflict,
    AnswerStale,
    AnswerStateConflict,
    NodeMissing,
    RunMissing,
    UnanswerableWait,
)
from atelier2.application.cancel_agent_attempt import (
    AttemptAlreadyTerminal,
    AttemptMissing,
    AttemptNotCurrent,
    CancellationAccepted,
    CancellationRunMissing,
    CancellationStale,
    CommandConflict,
    ReplacementNotAllowed,
)
from atelier2.application.cancel_run import (
    CancelAccepted,
    CancelCommandConflict,
    CancelEndedRun,
    CancelNotCancellable,
    CancelOvertakenBySuccess,
    CancelRunMissing,
    CancelTerminalRetry,
    MalformedIdempotencyKey,
)
from atelier2.application.fork_run import (
    RunForkCapabilityUnavailable,
    RunForkCommandConflict,
    RunForkCreated,
    RunForkExecutorUnavailable,
    RunForkExisting,
    RunForkLoopUnsupported,
    RunForkNodeMissing,
    RunForkOriginMissing,
    RunForkOriginNotTerminal,
    RunForkPrefixNotReusable,
)
from atelier2.application.read_runs import (
    NodeDetailRead,
    NodeNotFound,
    RunNotFound,
    RunsListed,
)
from atelier2.application.read_work_item_snapshot import WorkItemNotInTracker
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationCommandConflict,
    ReconciliationDeterminationConflict,
    ReconciliationExistingApplied,
    ReconciliationExistingPending,
    ReconciliationExistingRejected,
    ReconciliationStale,
    ReconciliationTargetMissing,
)
from atelier2.application.reconcile_run import ReconcileRunRequest
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ProjectSourceNotConnected,
    ReadUnavailable,
    SourcePayloadMalformed,
    WriteUnavailable,
)
from atelier2.application.start_published_run import (
    AgentConfigurationRevisionMissing,
    AuthoredAgentBinding,
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
)
from atelier2.application.start_published_run import (
    AgentExecutorBindingUnavailable as StartAgentExecutorBindingUnavailable,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.effects import (
    EffectId,
    EffectIntentStateVersion,
    EffectResult,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    ReconcileActor,
    ReconcileCommandId,
)
from atelier2.contracts.executions import NodeExecutionId, WaitAnswerActor
from atelier2.contracts.orders import (
    ArtifactOrderValue,
    InlineOrderValue,
    WorkItemOrderValue,
)
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.run_cancellations import is_operator_run_cancel
from atelier2.contracts.run_projections import RunProjection
from atelier2.contracts.runs import RunId, RunState

router = APIRouter()


def _refuse_work_item_order(
    name: str,
    reason: (
        ProjectSourceNotConnected
        | WorkItemNotInTracker
        | SourcePayloadMalformed
        | ReadUnavailable
    ),
) -> NoReturn:
    """Answer why the start could not read the item one order named.

    The reasons are the connected project's own, so they answer in the same
    problems the import door already publishes for them; only the item that
    was not there is about this order, and it is the order the caller fixes.
    """

    named = f"order {name!r}"
    match reason:
        case ProjectSourceNotConnected():
            raise ApiProblem("project-source-not-connected")
        case WorkItemNotInTracker(item_reference):
            raise ApiProblem(
                "run-input-refused",
                detail=(
                    f"{named} names {item_reference.tracker_item.value!r}, "
                    "which the connected tracker does not hold"
                ),
            )
        case SourcePayloadMalformed(detail):
            raise ApiProblem("project-source-payload-malformed", f"{named}: {detail}")
        case ReadUnavailable(detail):
            raise ApiProblem(
                "project-source-unavailable",
                named if detail is None else f"{named}: {detail}",
            )
        case _ as unreachable:
            assert_never(unreachable)


def _authored_order(order: AnyStartRunOrderResource) -> AuthoredOrder:
    """The order one wire shape is, in the vocabulary the start speaks."""
    match order:
        case InlineOrderResource(name=name, value=value):
            return AuthoredOrder(name, InlineOrderValue(value.encode()))
        case ArtifactOrderResource(name=name, artifact_hash=artifact_hash):
            return AuthoredOrder(name, ArtifactOrderValue(ArtifactHash(artifact_hash)))
        case WorkItemOrderResource(name=name, work_item=work_item):
            return AuthoredOrder(
                name, WorkItemOrderValue(TrackerItemReference(work_item))
            )
        case _ as unreachable:
            assert_never(unreachable)


@router.post(
    API_PREFIX + "/runs",
    response_model=RunResourceV3,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": RunResourceV3}},
)
async def start_run_route(
    body: AnyStartRunRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = RunId(body.run_id)
    require_new_run_identity(run_id, context.limits)
    revision_hash = parse_revision_hash(body.workflow_revision_hash)
    bindings = None
    orders: tuple[AuthoredOrder, ...] = ()
    if isinstance(body, (StartRunRequestResourceV2, StartRunRequestResourceV3)):
        bindings = tuple(
            AuthoredAgentBinding(
                binding.role, binding.agent_configuration_revision_hash
            )
            for binding in body.agent_bindings
        )
    if isinstance(body, StartRunRequestResourceV3):
        orders = tuple(_authored_order(order) for order in body.orders)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.start_published_run(
            run_id, revision_hash, bindings, orders
        ),
    )
    match result:
        case RunCreated():
            status = HTTPStatus.CREATED
        case RunExisting():
            status = HTTPStatus.OK
        case RevisionMissing():
            raise ApiProblem("workflow-revision-not-found")
        case RunIdentityConflict():
            raise ApiProblem("run-identity-conflict")
        case RunFormatNotExecutable():
            raise ApiProblem("workflow-format-not-executable")
        case RunInputRefused(name, refusal, detail, violation):
            # The input is named first because it is what an operator fixes; the
            # refusal token says which of the named ways it is wrong, and detail
            # -- when the refusal has one -- says where and why. A violation
            # that names one addressable field also reaches `invalid_fields`,
            # the mechanism every other field-level refusal already answers
            # through, so a caller need not parse this sentence to find it.
            sentence = f"input {name!r} was refused: {refusal}"
            if detail is not None:
                sentence = f"{sentence}: {detail}"
            invalid_fields = None
            if violation is not None and violation.pointer is not None:
                invalid_fields = (
                    bounded_invalid_field(violation.pointer, violation.reason),
                )
            raise ApiProblem(
                "run-input-refused", detail=sentence, invalid_fields=invalid_fields
            )
        case WorkItemOrderUnreadable(name, reason):
            _refuse_work_item_order(name, reason)
        case InvalidAgentBindings():
            raise ApiProblem("invalid-agent-bindings")
        case UncastAgentRoles(roles):
            raise ApiProblem(
                "uncast-agent-roles",
                uncast_roles=tuple(
                    UncastRoleResource(
                        role=role.role,
                        reason=role.reason.value,
                        family_differs_from=role.family_differs_from,
                    )
                    for role in roles
                ),
            )
        case BindingConstraintRefused(node, distinct_from):
            raise ApiProblem(
                "binding-constraint-refused",
                detail=(
                    f"node {node!r} declares distinct_from {distinct_from!r} "
                    "and both resolved to the same binding"
                ),
            )
        case AgentConfigurationRevisionMissing():
            raise ApiProblem("agent-configuration-revision-not-found")
        case StartAgentExecutorBindingUnavailable():
            raise ApiProblem("agent-executor-binding-unavailable")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


@router.get(API_PREFIX + "/runs", response_model=VersionedRunPageResource)
async def list_runs(
    after: PublicRunReferenceQueryParameter | None = None,
    limit: PageLimitParameter = DEFAULT_PAGE_LIMIT,
    state: str | None = None,
    context: ApiContext = api_context_dependency,
) -> VersionedRunPageResource:
    boundary = None
    if after is not None:
        boundary = decode_public_reference(after, context.limits)
    parsed_state = None
    if state is not None:
        try:
            parsed_state = RunState(state)
        except ValueError:
            raise ApiProblem(
                "invalid-request",
                invalid_fields=(
                    InvalidFieldResource(
                        path="query/state",
                        reason="not a run state this list can filter",
                    ),
                ),
            ) from None
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_runs(boundary, limit, parsed_state),
    )
    match result:
        case RunsListed(runs, next_after):
            require_run_projections(
                tuple(row for row in runs if isinstance(row, RunProjection)),
                context.limits,
            )
            return VersionedRunPageResource(
                items=tuple(run_list_row_resource(row) for row in runs),
                next_after=(
                    None
                    if next_after is None
                    else encode_public_run_reference(next_after)
                ),
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(API_PREFIX + "/runs/{public_ref}", response_model=RunResourceV3)
async def get_run_route(
    public_ref: PublicRunReferencePathParameter,
    context: ApiContext = api_context_dependency,
) -> RunResourceV3:
    return await _run_resource_of(
        decode_public_reference(public_ref, context.limits), context
    )


@router.post(
    API_PREFIX + "/runs/{public_ref}/forks",
    response_model=RunResourceV3,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": RunResourceV3}},
)
async def fork_run_route(
    public_ref: PublicRunReferencePathParameter,
    body: ForkRunRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    origin_run_id = decode_public_reference(public_ref, context.limits)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.fork_run(
            origin_run_id, body.idempotency_key, body.restart_from_node_id
        ),
    )
    match result:
        case RunForkCreated(_fork, run):
            status = HTTPStatus.CREATED
        case RunForkExisting(_fork, run):
            status = HTTPStatus.OK
        case RunForkOriginMissing():
            raise ApiProblem("run-not-found")
        case RunForkOriginNotTerminal():
            raise ApiProblem("run-fork-origin-not-terminal")
        case RunForkNodeMissing():
            raise ApiProblem("run-fork-node-missing")
        case RunForkLoopUnsupported():
            raise ApiProblem("run-fork-loop-unsupported")
        case RunForkPrefixNotReusable():
            raise ApiProblem("run-fork-prefix-not-reusable")
        case RunForkCommandConflict():
            raise ApiProblem("run-fork-command-conflict")
        case RunForkExecutorUnavailable():
            raise ApiProblem("agent-executor-binding-unavailable")
        case RunForkCapabilityUnavailable():
            raise ApiProblem("agent-executor-binding-unavailable")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run.run_id, context), status)


@router.get(
    API_PREFIX + "/runs/{public_ref}/nodes/{node_id}",
    response_model=NodeDetailResource,
)
async def get_node_detail_route(
    public_ref: PublicRunReferencePathParameter,
    node_id: str,
    context: ApiContext = api_context_dependency,
) -> NodeDetailResource:
    """What one node was asked, what it answered, who did it, and what stops it.

    The click into a node an operator makes on the run page, answered as one
    read: a panel that had to stitch this together from the run, the events and
    the receipts would be deriving what the server already knows.
    """

    run_id = decode_public_reference(public_ref, context.limits)
    try:
        context.limits.require_field(node_id)
    except ApiLimitExceeded as error:
        raise ApiProblem("node-not-found") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_node_detail(run_id, node_id),
    )
    match result:
        case NodeDetailRead(detail):
            return node_detail_resource(detail)
        case NodeNotFound():
            raise ApiProblem("node-not-found")
        case RunNotFound():
            raise ApiProblem("run-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.post(
    API_PREFIX + "/runs/{public_ref}/agent-attempts/{attempt_id}/cancellations",
    response_model=RunResourceV3,
    status_code=HTTPStatus.ACCEPTED,
    responses={HTTPStatus.OK: {"model": RunResourceV3}},
)
async def cancel_agent_attempt_route(
    public_ref: PublicRunReferencePathParameter,
    attempt_id: AgentAttemptIdPathParameter,
    body: CancelAgentAttemptRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = decode_public_reference(public_ref, context.limits)
    if is_operator_run_cancel(body.command_id):
        # #439's namespace invariant, closed from this side: an attempt-route
        # command id from the reserved run-cancel namespace could never have
        # been minted by an operator confirming *this* command (that mint
        # only happens server-side, from a run-cancel idempotency key), so it
        # names a command this route can never own.
        raise ApiProblem(
            "invalid-request",
            invalid_fields=(
                InvalidFieldResource(
                    path="body/command_id",
                    reason="belongs to the reserved run-cancel command namespace",
                ),
            ),
        )
    try:
        context.limits.require_field(attempt_id)
        parsed_attempt_id = AgentAttemptId(attempt_id)
        request = CancelAgentAttemptRequest(
            run_id,
            parsed_attempt_id,
            body.command_id,
            body.expected_attempt_state_version,
            AgentAttemptReplacement(body.replacement),
        )
    except (ApiLimitExceeded, TypeError, ValueError) as error:
        raise ApiProblem("invalid-agent-attempt-id") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.cancel_agent_attempt(request),
    )
    match result:
        case CancellationAccepted(terminal=terminal):
            status = HTTPStatus.OK if terminal else HTTPStatus.ACCEPTED
        case CancellationRunMissing():
            raise ApiProblem("run-not-found")
        case AttemptMissing():
            raise ApiProblem("agent-attempt-not-found")
        case AttemptNotCurrent():
            raise ApiProblem("agent-attempt-not-current")
        case CancellationStale():
            raise ApiProblem("agent-attempt-cancellation-stale")
        case AttemptAlreadyTerminal():
            raise ApiProblem("agent-attempt-terminal")
        case CommandConflict():
            raise ApiProblem("cancellation-command-conflict")
        case ReplacementNotAllowed():
            raise ApiProblem("replacement-not-allowed")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


@router.post(
    API_PREFIX + "/runs/{public_ref}/answers",
    response_model=RunResourceV3,
    status_code=HTTPStatus.ACCEPTED,
    responses={HTTPStatus.OK: {"model": RunResourceV3}},
)
async def answer_run_route(
    public_ref: PublicRunReferencePathParameter,
    body: AnswerWaitRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = decode_public_reference(public_ref, context.limits)
    require_fields(context.limits, body.node_id)
    revision_hash = parse_revision_hash(body.workflow_revision_hash)
    answer_bytes = decode_base64(body.answer_base64, context.limits)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.answer_wait(
            run_id,
            revision_hash,
            body.node_id,
            NodeExecutionId(body.expected_node_execution_id),
            WaitAnswerActor(body.actor),
            answer_bytes,
        ),
    )
    match result:
        case UnanswerableWait():
            raise ApiProblem("invalid-request")
        case AnswerAcceptedPending() | AnswerExistingPending():
            status = HTTPStatus.ACCEPTED
        case AnswerExistingApplied():
            status = HTTPStatus.OK
        case AnswerActorMismatch(expected_actor):
            raise ApiProblem(
                "invalid-request",
                invalid_fields=(
                    InvalidFieldResource(
                        path="body/actor",
                        reason=f"waiting execution expects {expected_actor.value!r}",
                    ),
                ),
            )
        case RunMissing():
            raise ApiProblem("run-not-found")
        case NodeMissing():
            raise ApiProblem("node-not-found")
        case AnswerRevisionConflict():
            raise ApiProblem("answer-revision-conflict")
        case AnswerStateConflict():
            raise ApiProblem("answer-state-conflict")
        case AnswerStale():
            raise ApiProblem("answer-execution-stale")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


@router.post(
    API_PREFIX + "/runs/{public_ref}/reconciliations",
    response_model=RunResourceV3,
    status_code=HTTPStatus.ACCEPTED,
    responses={HTTPStatus.OK: {"model": RunResourceV3}},
)
async def reconcile_run_route(
    public_ref: PublicRunReferencePathParameter,
    body: ReconcileRunRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    run_id = decode_public_reference(public_ref, context.limits)
    require_fields(
        context.limits,
        body.command_id,
        body.actor,
        body.evidence,
    )
    determination_body = body.determination
    if isinstance(determination_body, OperatorFoundDeterminationResource):
        require_field(determination_body.effect_id, context.limits)
        determination = OperatorFoundEffect(
            EffectId(determination_body.effect_id),
            EffectResult(
                decode_base64(determination_body.result_base64, context.limits)
            ),
        )
    else:
        determination = OperatorAuthoritativeAbsence()
    reconciliation_request = ReconcileRunRequest(
        run_id,
        ReconcileCommandId(body.command_id),
        EffectIntentStateVersion(body.expected_intent_state_version),
        ReconcileActor(body.actor),
        body.evidence,
        determination,
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.reconcile_run(reconciliation_request),
    )
    match result:
        case ReconciliationAcceptedPending() | ReconciliationExistingPending():
            status = HTTPStatus.ACCEPTED
        case ReconciliationExistingApplied():
            status = HTTPStatus.OK
        case RunMissing():
            raise ApiProblem("run-not-found")
        case ReconciliationTargetMissing():
            raise ApiProblem("reconciliation-target-missing")
        case ReconciliationStale():
            raise ApiProblem("reconciliation-stale")
        case ReconciliationCommandConflict():
            raise ApiProblem("reconciliation-command-conflict")
        case ReconciliationDeterminationConflict():
            raise ApiProblem("reconciliation-determination-conflict")
        case ReconciliationExistingRejected():
            raise ApiProblem("reconciliation-rejected")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("durable-projection-unrepresentable")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


@router.post(
    API_PREFIX + "/runs/{public_ref}/cancellations",
    response_model=RunResourceV3,
    status_code=HTTPStatus.ACCEPTED,
    responses={HTTPStatus.OK: {"model": RunResourceV3}},
)
async def cancel_run_route(
    public_ref: PublicRunReferencePathParameter,
    body: CancelRunRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    """Cancel one honestly cancellable V3 run under one operator command.

    The client carries only its `idempotency_key`; the durable command id is
    minted server-side into the reserved run-cancel namespace, so no request
    field can force or bypass it. `expected_node_execution_id` is #439 D2's
    fence, so a confirmation read in one loop round cannot stop another round's
    attempt.
    """
    run_id = decode_public_reference(public_ref, context.limits)
    expected_node_execution_id = NodeExecutionId(body.expected_node_execution_id)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.cancel_run(
            run_id, body.idempotency_key, expected_node_execution_id
        ),
    )
    match result:
        case CancelAccepted():
            status = HTTPStatus.ACCEPTED
        case CancelEndedRun() | CancelTerminalRetry():
            # The run is already over when either answer is given, so there is
            # nothing left to accept: the reader gets the ended run itself.
            status = HTTPStatus.OK
        case CancelOvertakenBySuccess():
            raise ApiProblem("run-cancellation-overtaken-by-success")
        case CancelNotCancellable(reason):
            raise ApiProblem(
                "run-not-cancellable",
                detail=f"This run cannot be cancelled right now: {reason}.",
            )
        case CancelCommandConflict():
            raise ApiProblem("run-cancellation-command-conflict")
        case CancelRunMissing():
            raise ApiProblem("run-not-found")
        case MalformedIdempotencyKey():
            raise ApiProblem(
                "invalid-request",
                invalid_fields=(
                    InvalidFieldResource(
                        path="body/idempotency_key",
                        reason="no run-cancel command can be minted from this key",
                    ),
                ),
            )
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(await _run_resource_of(run_id, context), status)


async def _run_resource_of(run_id: RunId, context: ApiContext) -> RunResourceV3:
    return await load_run_resource(
        run_id,
        context.use_cases.get_run,
        context.control_runner,
        context.limits,
    )
