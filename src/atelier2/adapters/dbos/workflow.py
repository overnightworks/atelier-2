from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, assert_never, cast

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID, SQLAlchemyDatasource

from atelier2.adapters.attempt_workspace_files import AttemptWorkspaceFileAccess
from atelier2.adapters.dbos.advancer import (
    prepare_graph_action,
    prepare_graph_agent_open_pr,
    prepare_graph_agent_push,
    read_pinned_effect_tool_grant,
    read_pinned_exec_tool_grant,
    redeem_agent_effect,
)
from atelier2.adapters.dbos.agent_attempt_store import (
    compose_agent_node_job_for_attempt,
    load_prior_output_schema_refusal_receipt,
)
from atelier2.adapters.dbos.continuation import (
    checkpoint_confirmed_effect,
    schedule_confirmed_effect_continuation,
)
from atelier2.adapters.dbos.effect_store import (
    EncodedEffectResolution,
    commit_resolution,
    load_intent,
    observe_adapter_with_fork_fence,
    observe_reconcile_command,
    resolve_observation,
)
from atelier2.adapters.dbos.names import (
    ACTION_CONTINUATION_WORKFLOW_NAME,
    ACTION_PREPARE_STEP_NAME,
    AGENT_EFFECT_PREPARE_STEP_NAME,
    AGENT_EFFECT_REDEEM_STEP_NAME,
    ANSWER_COMMIT_STEP_NAME,
    ANSWER_WORKFLOW_NAME,
    BOOTSTRAP_STEP_NAME,
    CANCELLATION_WORKFLOW_NAME,
    COMMIT_STEP_NAME,
    EFFECT_WORKFLOW_NAME,
    NODE_BINDING_STEP_NAME,
    NODE_WORKFLOW_NAME,
    OBSERVE_STEP_NAME,
    RECONCILE_WORKFLOW_NAME,
    REPLACEMENT_WORKFLOW_NAME,
    RESOLVE_STEP_NAME,
    SUBWORKFLOW_COMMIT_STEP_NAME,
    SUBWORKFLOW_WORKFLOW_NAME,
    WAIT_COMMIT_STEP_NAME,
    WORKFLOW_NAME,
)
from atelier2.adapters.dbos.node_binding_codec import (
    EncodedNodeBinding,
    decode_node_binding,
    encode_node_binding,
)
from atelier2.adapters.dbos.run_store import (
    bootstrap_node_for_snapshot,
    commit_subworkflow_completed,
    commit_wait_answered,
    load_node_outputs,
    load_published_schema_document,
    load_run_inputs,
    load_wait_answer,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    commit_waiting_input,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    published_revisions,
    reconcile_commands,
)
from atelier2.adapters.dbos.work_item_claims import (
    WorkItemClaimLedger,
    hold_work_item_claim,
)
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    node_workflow_id_for,
    subworkflow_workflow_id_for,
)
from atelier2.application.bind_node import (
    agent_execution_request_v2,
    bind_node,
    pinned_project,
    require_the_run_stands_on,
)
from atelier2.application.cancel_agent_attempt import (
    continue_agent_attempt_cancellation,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttempt,
    AgentAttemptId,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agent_permissions import PermissionPolicyRevision
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.budgets_v3 import (
    BudgetRevisionRefused,
    read_budget_revision_document,
)
from atelier2.contracts.effects import (
    EffectAdapterBinding,
    EffectIntent,
    LogicalEffectKey,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.node_bindings import (
    ActionNodeBinding,
    AgentNodeBindingV2,
    SubworkflowNodeBinding,
    WaitNodeBinding,
)
from atelier2.contracts.node_records_v3 import DeliveredOutput, RunInput
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
)
from atelier2.contracts.workflows import (
    RunCompletes,
    RunContinues,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    AnyWorkflowDocumentNode,
    WaitNodeV3,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptStore,
    AgentAttemptSucceeded,
    AgentExecutorBindingRefusalFenced,
    AgentExecutorBindingRefusalNeedsPreparedCleanup,
    AgentExecutorBindingRefusalWritten,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceOwner,
    AgentExecutorCarrier,
    AgentExecutorKey,
    AgentExecutorV2,
    AgentSession,
)
from atelier2.ports.artifacts import ArtifactPublisher
from atelier2.ports.effects import EffectAdapter, OpenEffectAdapterRegistry
from atelier2.ports.project_verification import (
    DeclaredProject,
)

CANCELLATION_REDRIVE_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0)

_LOG = logging.getLogger("atelier2")


def _declared_workspace_owner(
    owner: AgentAttemptWorkspaceOwner | None,
) -> AgentAttemptWorkspaceOwner:
    if owner is None:
        raise RunBindingConflict(
            "an agent node requires the declared agent scratch root"
        )
    return owner


def _declared_agent_session(
    session: AgentSession | None,
) -> AgentSession:
    if session is None:
        raise RunBindingConflict(
            "an agent node requires the declared local agent session"
        )
    return session


def bootstrap_run_binding(
    datasource: SQLAlchemyDatasource,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
) -> str:
    def load_binding() -> str:
        session = datasource.sql_session()
        run = load_run(session, run_id)
        if run.revision_hash != revision_hash:
            raise RunBindingConflict("bootstrap requires its exact durable run binding")
        graph = load_graph(session, revision_hash)
        return bootstrap_node_for_snapshot(session, run, graph)

    return str(datasource.run_tx_step({"name": BOOTSTRAP_STEP_NAME}, load_binding))


def _run_effect_step(
    datasource: SQLAlchemyDatasource,
    name: str,
    operation: Any,
    *arguments: Any,
) -> EncodedEffectResolution:
    def execute() -> EncodedEffectResolution:
        return operation(datasource.sql_session(), *arguments)

    return datasource.run_tx_step({"name": name}, execute)


def _node_binding(
    datasource: SQLAlchemyDatasource,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    project: DeclaredProject | None,
) -> EncodedNodeBinding:
    """Record what this node durably binds, including the source it is pinned to.

    The pin is taken here and nowhere else. Composing the binding is the one
    moment a node's material is decided, so resolving the project's head again at
    launch time would let a commit landing between the two silently change what a
    started run works on -- and a recovered node replays the binding this step
    recorded rather than resolving anything a second time.
    """

    def load() -> EncodedNodeBinding:
        session = datasource.sql_session()
        run = load_run(session, run_id)
        require_the_run_stands_on(run, revision_hash, node_id)
        graph = load_graph(session, revision_hash)
        node = graph.node(node_id)
        orders, results = _node_material(
            session, run_id, revision_hash, graph, node, run.current_round_ordinal
        )
        return encode_node_binding(
            bind_node(
                run,
                node,
                orders=orders,
                results=results,
                tool_grant=_pinned_tool_grant(session, node),
                declared_output_schema_document=_declared_output_schema_document(
                    session, node
                ),
                maximum_assistant_turns=_pinned_maximum_assistant_turns(session, node),
                project_source=_pinned_source(node, project),
            )
        )

    return cast(
        EncodedNodeBinding,
        datasource.run_tx_step({"name": NODE_BINDING_STEP_NAME}, load),
    )


def _node_material(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    graph: AnyWorkflowDocument,
    node: AnyWorkflowDocumentNode,
    round_ordinal: int,
) -> tuple[tuple[RunInput, ...], tuple[DeliveredOutput, ...]]:
    """The orders and earlier results a V3 Agent or Wait reads.

    Read here rather than inside the decision because reading them is a store
    call. Other node kinds have no composed request and receive empty material.

    The round travels with the read because a looped producer wrote one value per
    round: without it the store would be asked for a node's output and hold
    several.
    """
    if not isinstance(node, (AgentNodeV3, WaitNodeV3)):
        return ((), ())
    return (
        load_run_inputs(session, run_id, node),
        load_node_outputs(session, run_id, revision_hash, graph, node, round_ordinal),
    )


def _pinned_source(
    node: AnyWorkflowDocumentNode, project: DeclaredProject | None
) -> ProjectSourcePin | None:
    """The source this runtime pins for one Agent node, resolved once and here.

    Only an Agent node works in a tree, so only an Agent node's binding takes the
    head -- a Wait or Subworkflow node that resolved it would make a run depend
    on a repository it never reads.
    """
    if project is None or not isinstance(node, AgentNodeV3):
        return None
    return project.source.head()


def _executor_key(binding: AgentNodeBindingV2) -> AgentExecutorKey:
    """Which registered executor this binding names: its provider and its revision."""
    return AgentExecutorKey(
        binding.resolved.auth_profile.provider_id,
        binding.resolved.configuration.executor_revision,
    )


AgentExecutorMap = Mapping[
    AgentExecutorKey,
    tuple[
        AgentExecutorV2 | None,
        AgentExecutorOperationalIdentity,
        frozenset[AgentExecutionCapability],
        AgentExecutorCarrier,
    ],
]


@dataclass(frozen=True, slots=True)
class ReconstructedAgentAttempt:
    """One durable Attempt rebuilt into everything a driver or a converger needs.

    The `execution` is the sole reason this exists as one owner: an
    `AgentExecutionRequestV2` is re-derived from durable truth through the same
    `_node_binding`/`bind_node` composition that first minted it, so its
    `request_hash` is byte-identical to the one the durable attempt already
    carries, so a replacement workflow never commits evidence under a
    drifting, independently re-derived request. `binding`, `executor` and
    `carrier` are the rest of what an executing caller (the replacement
    workflow) needs, so it never decodes the same binding twice.
    """

    execution: AgentAttemptExecution
    binding: AgentNodeBindingV2
    executor: AgentExecutorV2 | None
    carrier: AgentExecutorCarrier


def _reconstructed_agent_repair_job(
    datasource: SQLAlchemyDatasource,
    attempt: AgentAttempt,
    round_ordinal: int,
) -> str | None:
    """Read and compose a repair only when its durable refusal receipt exists.

    Ordinal two predates schema repair: cancellation also mints one replacement.
    The receipt, not the ordinal, distinguishes the two.  It is deliberately
    loaded in one DBOS transaction step because replacement workflows may only
    access their datasource through that boundary.
    """

    def load() -> str | None:
        session = datasource.sql_session()
        graph = load_graph(session, attempt.workflow_revision_hash)
        node = graph.node(attempt.node_id)
        if not isinstance(node, AgentNodeV3):
            prior_receipt_attempt_id = session.scalar(
                sa.select(agent_attempt_receipts_v3.c.attempt_id)
                .select_from(
                    agent_attempt_receipts_v3.join(
                        agent_attempts,
                        agent_attempt_receipts_v3.c.attempt_id
                        == agent_attempts.c.attempt_id,
                    )
                )
                .where(
                    agent_attempts.c.node_execution_id
                    == attempt.node_execution_id.value,
                    agent_attempts.c.attempt_ordinal == AGENT_ATTEMPT_ORDINAL,
                )
            )
            if prior_receipt_attempt_id is not None:
                raise RunTransitionConflict(
                    "repair receipt belongs to a node that is not an agent node"
                )
            return None
        receipt = load_prior_output_schema_refusal_receipt(
            session,
            target_attempt_id=attempt.attempt_id,
            target_node_execution_id=attempt.node_execution_id,
            target_attempt_ordinal=attempt.attempt_ordinal,
            expected_schema_revision=PublishedRevisionHash(
                node.outputs[0].schema_reference.revision
            ),
        )
        if receipt is None:
            return None
        orders, results = _node_material(
            session,
            attempt.run_id,
            attempt.workflow_revision_hash,
            graph,
            node,
            round_ordinal,
        )
        return compose_agent_node_job_for_attempt(
            node,
            orders,
            results,
            target_node_execution_id=attempt.node_execution_id,
            target_attempt_ordinal=attempt.attempt_ordinal,
            prior_refusal_receipt=receipt,
        ).decode("utf-8")

    return cast(
        str | None,
        datasource.run_tx_step({"name": "reconstruct-agent-job"}, load),
    )


def reconstruct_agent_attempt(
    datasource: SQLAlchemyDatasource,
    agent_executors_v2: AgentExecutorMap,
    project: DeclaredProject | None,
    attempt: AgentAttempt,
) -> ReconstructedAgentAttempt:
    """Rebuild one durable Attempt's execution from durable state, exactly.

    The one owner of this reconstruction: the replacement workflow drives the
    result, and a Serve-restart convergence (`#585`) reads its `execution` to
    commit a retained terminal fact. Reuse rather than duplication is the whole
    point -- the `request_hash` this yields has to match the durable attempt's,
    and two derivations that drift would converge nothing.
    """
    binding = decode_node_binding(
        _node_binding(
            datasource,
            attempt.run_id,
            attempt.workflow_revision_hash,
            attempt.node_id,
            project,
        )
    )
    if not isinstance(binding, AgentNodeBindingV2):
        raise RunTransitionConflict("durable attempt is not a V2 agent node")
    if attempt.attempt_ordinal == REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
        repair_job = _reconstructed_agent_repair_job(
            datasource, attempt, binding.round_ordinal
        )
        if repair_job is not None:
            binding = replace(
                binding,
                job=repair_job,
            )
    executor, operational_identity, declared_capabilities, carrier = agent_executors_v2[
        _executor_key(binding)
    ]
    request = agent_execution_request_v2(
        binding,
        attempt.run_id,
        attempt.workflow_revision_hash,
        attempt.node_id,
        operational_identity,
        declared_capabilities,
    )
    if (
        request.node_execution_id != attempt.node_execution_id
        or request.request_hash != attempt.request_hash
        or request.executor_operational_identity
        != attempt.executor_operational_identity
    ):
        raise RunTransitionConflict(
            "reconstructed request differs from its durable attempt binding"
        )
    return ReconstructedAgentAttempt(
        AgentAttemptExecution(request, attempt.attempt_id, attempt.attempt_ordinal),
        binding,
        executor,
        carrier,
    )


def _pinned_tool_grant(
    session: Any, node: AnyWorkflowDocumentNode
) -> DeclaredToolGrant | None:
    """The exec-shaped grant this node's binding carries, or nothing.

    Only an exec-shaped grant is redeemed inside the attempt's lease, so only it
    travels in the binding beside the `project_source` it needs. An
    effect-shaped grant -- an `open-pr` a pull request answers -- carries no
    source and is redeemed after the attempt succeeds, straight from the
    immutable graph where its effect is prepared, so the binding leaves it out
    rather than force it a `project_source` it has no use for.
    """
    return read_pinned_exec_tool_grant(session, node)


def _pinned_maximum_assistant_turns(
    session: Any, node: AnyWorkflowDocumentNode
) -> int | None:
    """The turn bound this node pinned, read from the revision the document pins.

    Same door as the tool grant: the run already resolved these bytes as a budget
    before any process existed, so a registry that cannot answer for them now,
    or answers with bytes that are no budget, contradicts a run that started.
    A budget that names no turn bound is still a budget; the executor then keeps
    the default it already declares.
    """
    if not isinstance(node, AgentNodeV3) or node.budget is None:
        return None
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.BUDGET_POLICY.value,
            published_revisions.c.revision_hash == node.budget.revision,
        )
    )
    if document is None:
        raise RunBindingConflict("the pinned budget revision left the registry")
    verdict = read_budget_revision_document(bytes(document))
    if isinstance(verdict, BudgetRevisionRefused):
        raise RunBindingConflict(f"the pinned budget revision is no budget: {verdict}")
    return verdict.content.maximum_assistant_turns


def _declared_output_schema_document(
    session: Any, node: AnyWorkflowDocumentNode
) -> str | None:
    """The exact published document this node's one output pinned, as its own text.

    Same bytes the schema seam later judges. The binding carries them so the
    provider flag cannot invent a second serialization, and the text is decoded
    here because this is the only place that knows which node pinned it.
    """
    if not isinstance(node, AgentNodeV3):
        return None
    if not node.outputs:
        raise RunBindingConflict("a V3 agent node has no declared output schema")
    declared = node.outputs[0]
    document = load_published_schema_document(
        session, declared.schema_reference.revision
    )
    if document is None:
        raise RunBindingConflict(
            f"the schema node {node.id!r} pinned for output "
            f"{declared.name!r} left the registry"
        )
    try:
        return document.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunBindingConflict(
            f"the schema node {node.id!r} pinned for output "
            f"{declared.name!r} is not UTF-8"
        ) from error


def register_durable_run_workflow(
    datasource: SQLAlchemyDatasource,
    agent_executors_v2: Mapping[
        AgentExecutorKey,
        tuple[
            AgentExecutorV2 | None,
            AgentExecutorOperationalIdentity,
            frozenset[AgentExecutionCapability],
            AgentExecutorCarrier,
        ],
    ],
    agent_attempt_store: AgentAttemptStore,
    agent_session: AgentSession | None,
    agent_permission_policy: PermissionPolicyRevision,
    agent_workspace_owner: AgentAttemptWorkspaceOwner | None,
    project: DeclaredProject | None,
    artifact_publisher: ArtifactPublisher,
    adapter: OpenEffectAdapterRegistry,
    effect_binding: tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None = None,
    work_item_claims: WorkItemClaimLedger | None = None,
) -> None:
    effect_bindings = effect_binding

    def adapter_for_intent(intent: EffectIntent) -> EffectAdapter:
        return adapter.adapter_for(
            intent.binding.operation_name, intent.binding.adapter_binding
        )

    def adapter_for_key(logical_key: str, revision_hash: str) -> EffectAdapter:
        intent = datasource.run_tx_step(
            {"name": "effect-adapter-binding"},
            lambda: load_intent(datasource.sql_session(), logical_key, revision_hash),
        )
        return adapter_for_intent(intent)

    def execute_v2_attempt(
        attempt_execution: AgentAttemptExecution,
        executor: AgentExecutorV2,
        binding: AgentNodeBindingV2,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed | AgentAttemptPossiblyRan:
        return execute_agent_attempt(
            attempt_execution,
            executor,
            agent_attempt_store,
            _declared_agent_session(agent_session),
            _declared_workspace_owner(agent_workspace_owner),
            pinned_project(binding, project),
            artifact_publisher,
            permissions=agent_permission_policy,
            workspace_files=AttemptWorkspaceFileAccess,
        )

    def agent_node_attempt(
        binding: AgentNodeBindingV2,
        run_id: RunId,
        revision_hash: WorkflowRevisionHash,
        node_id: str,
        attempt_ordinal: int,
    ) -> ReconstructedAgentAttempt:
        """One turn at an Agent node: what it executes, and who executes it.

        Derived from the binding the caller already decoded rather than from a
        durable attempt, because the first turn mints the attempt this names --
        it does not read one back.
        """

        executor, operational_identity, declared_capabilities, carrier = (
            agent_executors_v2[_executor_key(binding)]
        )
        request = agent_execution_request_v2(
            binding,
            run_id,
            revision_hash,
            node_id,
            operational_identity,
            declared_capabilities,
        )
        return ReconstructedAgentAttempt(
            AgentAttemptExecution(
                request,
                AgentAttemptId.for_execution(
                    request.node_execution_id, request.request_hash, attempt_ordinal
                ),
                attempt_ordinal,
            ),
            binding,
            executor,
            carrier,
        )

    def drive_agent_node(
        binding: AgentNodeBindingV2,
        run_id: RunId,
        revision_hash: WorkflowRevisionHash,
        node_id: str,
    ) -> str:
        """One Agent node from its preconditions to wherever the run stands next.

        The executor is asked for first: a node no bound executor can start
        ends on that, and posting a claim for work this host cannot begin
        would leave a lane held for nothing.
        """

        attempt = agent_node_attempt(
            binding, run_id, revision_hash, node_id, AGENT_ATTEMPT_ORDINAL
        )
        if attempt.executor is None:
            return refuse_unavailable_executor(attempt.execution.request)
        unclaimed = hold_work_item_claim(
            datasource,
            work_item_claims,
            project_id,
            binding,
            run_id,
            revision_hash,
            node_id,
        )
        if unclaimed is not None:
            return unclaimed
        outcome = execute_v2_attempt(attempt.execution, attempt.executor, binding)
        return continue_run_after(
            outcome, binding, run_id, revision_hash, node_id, binding.round_ordinal
        )

    def continue_run_after(
        outcome: AgentAttemptExecutionOutcome,
        node_binding: AgentNodeBindingV2,
        run_id: RunId,
        revision_hash: WorkflowRevisionHash,
        node_id: str,
        round_ordinal: int,
    ) -> str:
        """Redeem what this Attempt earned and start whatever the run does next.

        Where the run stands afterwards is the answer, so the one caller that
        owes a different word -- the replacement workflow, which answers with its
        Attempt's own state -- takes the side effects and ignores the word.
        """

        if not isinstance(outcome, AgentAttemptSucceeded):
            return RunState.STARTED.value
        redeemed_effect = redeem_agent_node_effect(
            outcome, node_binding, run_id, revision_hash, node_id, round_ordinal
        )
        if redeemed_effect is not None:
            logical_key, state = redeemed_effect
            if state is RunState.WAITING_RECONCILIATION:
                return state.value
            return continue_confirmed_effect(logical_key, revision_hash)
        match outcome.completion:
            case RunContinues(successor_id, successor_round):
                start_node(run_id, revision_hash, successor_id, successor_round)
                return RunState.STARTED.value
            case RunCompletes():
                return RunState.COMPLETED.value
            case _ as unreachable:
                assert_never(unreachable)

    def redeem_agent_node_effect(
        outcome: AgentAttemptSucceeded,
        node_binding: AgentNodeBindingV2,
        run_id: RunId,
        revision_hash: WorkflowRevisionHash,
        node_id: str,
        round_ordinal: int,
    ) -> tuple[LogicalEffectKey, RunState] | None:
        """Redeem the external effect this agent node's own grant earned, if any.

        Runs after the attempt has durably succeeded and candidate capture has
        kept its tree. Only an effect-shaped grant prepares an intent. Preparing
        and redeeming are separate durable steps, so replay reads the standing
        remote effect back instead of creating a twin.
        """
        grant = datasource.run_tx_step(
            {"name": "agent-effect-kind"},
            lambda: read_pinned_effect_tool_grant(
                datasource.sql_session(),
                load_graph(datasource.sql_session(), revision_hash).node(node_id),
            ),
        )
        push = (
            grant is not None
            and grant.capability is ToolGrantCapability.PUSH_ATELIER_COMMIT
        )
        if push:
            if (
                project is None
                or project_id is None
                or node_binding.project_source is None
            ):
                raise RunBindingConflict("push grant requires its declared project")
            push_project = project
            push_project_id = project_id
            push_source = node_binding.project_source
            candidate = push_project.candidates.read(outcome.attempt.attempt_id)
            if candidate is None:
                raise RunBindingConflict("successful push attempt has no candidate")
            prepare = lambda: prepare_graph_agent_push(
                datasource.sql_session(),
                run_id,
                revision_hash,
                node_id,
                round_ordinal,
                outcome.attempt.attempt_id.value,
                candidate.tree,
                push_source.commit,
                effect_bindings,
                push_project_id,
            )
        else:
            prepare = lambda: prepare_graph_agent_open_pr(
                datasource.sql_session(),
                run_id,
                revision_hash,
                node_id,
                round_ordinal,
                effect_bindings,
                project_id,
            )
        logical_key = datasource.run_tx_step(
            {"name": AGENT_EFFECT_PREPARE_STEP_NAME},
            prepare,
        )
        if logical_key is None:
            return None
        selected_adapter = adapter_for_key(str(logical_key), revision_hash.value)
        state = RunState(
            datasource.run_tx_step(
                {"name": AGENT_EFFECT_REDEEM_STEP_NAME},
                lambda: redeem_agent_effect(
                    datasource.sql_session(),
                    selected_adapter,
                    str(logical_key),
                    revision_hash.value,
                ),
            )
        )
        if state not in {RunState.STARTED, RunState.WAITING_RECONCILIATION}:
            raise RunTransitionConflict(
                "agent effect redemption returned invalid state"
            )
        return LogicalEffectKey(str(logical_key)), state

    def continue_confirmed_effect(
        logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
    ) -> str:
        run_id, head, round_ordinal, state = checkpoint_confirmed_effect(
            datasource, logical_key, revision_hash
        )
        if RunState(state) is RunState.STARTED:
            start_node(RunId(run_id), revision_hash, head, round_ordinal)
        return state

    def start_node(
        run_id: RunId,
        revision_hash: WorkflowRevisionHash,
        node_id: str,
        round_ordinal: int = FIRST_ROUND_ORDINAL,
    ) -> None:
        """Start one round of one node under the identity that round has.

        The durable workflow id is derived from the execution, so a second round
        of the same node is a second durable workflow rather than a repeat of
        the first that the idempotency key would swallow.
        """
        execution_id = NodeExecutionId.for_node(
            run_id, revision_hash, node_id, round_ordinal
        )
        with SetWorkflowID(node_workflow_id_for(execution_id)):
            DBOS.start_workflow(
                durable_node, run_id.value, revision_hash.value, node_id
            )

    def refuse_unavailable_executor(request: AgentExecutionRequestV2) -> str:
        redrive_index = 0
        while True:
            refusal = agent_attempt_store.refuse_unavailable_executor(request)
            match refusal:
                case AgentExecutorBindingRefusalWritten():
                    return RunState.FAILED.value
                case AgentExecutorBindingRefusalNeedsPreparedCleanup(
                    _attempt, cleanup_request
                ):
                    accepted = agent_attempt_store.request_cancellation(cleanup_request)
                    if not isinstance(accepted, AgentAttemptCancellationAccepted):
                        DBOS.sleep(
                            CANCELLATION_REDRIVE_SECONDS[
                                min(
                                    redrive_index,
                                    len(CANCELLATION_REDRIVE_SECONDS) - 1,
                                )
                            ]
                        )
                        redrive_index += 1
                        continue
                    terminal = continue_agent_attempt_cancellation(
                        cleanup_request,
                        agent_attempt_store,
                        _declared_agent_session(agent_session),
                        _declared_workspace_owner(agent_workspace_owner),
                    )
                    if terminal is None:
                        DBOS.sleep(
                            CANCELLATION_REDRIVE_SECONDS[
                                min(
                                    redrive_index,
                                    len(CANCELLATION_REDRIVE_SECONDS) - 1,
                                )
                            ]
                        )
                        redrive_index += 1
                        continue
                    continue
                case AgentExecutorBindingRefusalFenced():
                    return RunState.STARTED.value
                case _ as unreachable:
                    assert_never(unreachable)

    @DBOS.workflow(name=WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_run(run_id: str, revision_hash: str) -> str:
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)
        start = bootstrap_run_binding(datasource, typed_run_id, typed_revision)
        start_node(typed_run_id, typed_revision, start)
        return RunState.STARTED.value

    @DBOS.workflow(name=SUBWORKFLOW_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_add(left: int, right: int) -> int:
        return left + right

    @DBOS.workflow(name=CANCELLATION_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_agent_attempt_cancellation(
        run_id: str, attempt_id: str, command_id: str
    ) -> str:
        attempt = agent_attempt_store.load(AgentAttemptId(attempt_id))
        cancellation = attempt.cancellation
        if cancellation is None or attempt.run_id != RunId(run_id):
            raise RunTransitionConflict(
                "cancellation workflow differs from its durable attempt"
            )
        request = CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            command_id,
            cancellation.expected_attempt_state_version,
            cancellation.replacement,
        )
        if attempt.runner_manifest_id is not None:
            raise RunTransitionConflict(
                "a runner-lease-bound attempt cannot cancel: the runner is "
                "deleted (#1252)"
            )
        redrive_index = 0
        while (
            continue_agent_attempt_cancellation(
                request,
                agent_attempt_store,
                _declared_agent_session(agent_session),
                _declared_workspace_owner(agent_workspace_owner),
            )
            is None
        ):
            DBOS.sleep(
                CANCELLATION_REDRIVE_SECONDS[
                    min(redrive_index, len(CANCELLATION_REDRIVE_SECONDS) - 1)
                ]
            )
            redrive_index += 1
        return agent_attempt_store.load(attempt.attempt_id).state.value

    @DBOS.workflow(name=REPLACEMENT_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_agent_attempt_replacement(attempt_id: str) -> str:
        replacement = agent_attempt_store.load(AgentAttemptId(attempt_id))
        reconstructed = reconstruct_agent_attempt(
            datasource, agent_executors_v2, project, replacement
        )
        if reconstructed.executor is None:
            return RunState.STARTED.value
        outcome = execute_v2_attempt(
            reconstructed.execution,
            reconstructed.executor,
            reconstructed.binding,
        )
        continue_run_after(
            outcome,
            reconstructed.binding,
            replacement.run_id,
            replacement.workflow_revision_hash,
            replacement.node_id,
            reconstructed.binding.round_ordinal,
        )
        # This workflow answers with its Attempt's own word rather than the run's,
        # read back from the store so the answer is the durable one whatever the
        # drive reported.
        return agent_attempt_store.load(replacement.attempt_id).state.value

    @DBOS.workflow(name=NODE_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_node(run_id: str, revision_hash: str, node_id: str) -> str:
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)
        binding = decode_node_binding(
            _node_binding(datasource, typed_run_id, typed_revision, node_id, project)
        )
        if isinstance(binding, AgentNodeBindingV2):
            return drive_agent_node(binding, typed_run_id, typed_revision, node_id)
        if isinstance(binding, ActionNodeBinding):
            logical_key = str(
                datasource.run_tx_step(
                    {"name": ACTION_PREPARE_STEP_NAME},
                    lambda: (
                        prepare_graph_action(
                            datasource.sql_session(),
                            typed_run_id,
                            typed_revision,
                            effect_bindings,
                            project_id,
                        ).intent.binding.logical_key.value
                    ),
                )
            )
            with SetWorkflowID(effect_workflow_id_for(LogicalEffectKey(logical_key))):
                DBOS.start_workflow(durable_effect, logical_key, revision_hash)
            return RunState.STARTED.value
        if isinstance(binding, WaitNodeBinding):
            wait_round_ordinal = binding.round_ordinal
            datasource.run_tx_step(
                {"name": WAIT_COMMIT_STEP_NAME},
                lambda: (
                    commit_waiting_input(
                        datasource.sql_session(),
                        typed_run_id,
                        typed_revision,
                        node_id,
                        wait_round_ordinal,
                        binding.question,
                    ).state.value
                ),
            )
            return RunState.WAITING_INPUT.value
        if isinstance(binding, SubworkflowNodeBinding):
            execution_id = NodeExecutionId.for_node(
                typed_run_id, typed_revision, node_id
            )
            with SetWorkflowID(subworkflow_workflow_id_for(execution_id)):
                handle = DBOS.start_workflow(durable_add, *binding.operands)
            result = handle.get_result()
            datasource.run_tx_step(
                {"name": SUBWORKFLOW_COMMIT_STEP_NAME},
                lambda: (
                    commit_subworkflow_completed(
                        datasource.sql_session(),
                        typed_run_id,
                        typed_revision,
                        node_id,
                        result,
                    ).state.value
                ),
            )
            return RunState.COMPLETED.value
        assert_never(binding)

    @DBOS.workflow(name=EFFECT_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_effect(logical_key: str, revision_hash: str) -> str:
        selected_adapter = adapter_for_key(logical_key, revision_hash)
        observed = _run_effect_step(
            datasource,
            OBSERVE_STEP_NAME,
            observe_adapter_with_fork_fence,
            selected_adapter,
            logical_key,
            revision_hash,
        )
        resolved = _run_effect_step(
            datasource,
            RESOLVE_STEP_NAME,
            resolve_observation,
            selected_adapter,
            logical_key,
            revision_hash,
            observed,
        )
        state = RunState(
            datasource.run_tx_step(
                {"name": COMMIT_STEP_NAME},
                lambda: (
                    commit_resolution(
                        datasource.sql_session(), logical_key, revision_hash, resolved
                    ).value
                ),
            )
        )
        if state is RunState.STARTED:
            schedule_confirmed_effect_continuation(
                durable_action_continuation,
                LogicalEffectKey(logical_key),
                WorkflowRevisionHash(revision_hash),
            )
        return state.value

    @DBOS.workflow(name=RECONCILE_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_reconciliation(command_id: str, revision_hash: str) -> str:
        command_logical_key = str(
            datasource.run_tx_step(
                {"name": "reconcile-adapter-binding"},
                lambda: datasource.sql_session().scalar(
                    sa.select(reconcile_commands.c.logical_key).where(
                        reconcile_commands.c.command_id == command_id
                    )
                ),
            )
        )
        selected_adapter = adapter_for_key(command_logical_key, revision_hash)
        observed = _run_effect_step(
            datasource,
            OBSERVE_STEP_NAME,
            observe_reconcile_command,
            selected_adapter,
            command_id,
            revision_hash,
        )
        command = ReconcileCommandId(command_id)
        logical_key = str(observed["logical_key"])
        resolved = _run_effect_step(
            datasource,
            RESOLVE_STEP_NAME,
            resolve_observation,
            selected_adapter,
            logical_key,
            revision_hash,
            observed,
            command if observed.get("operator_authorized") == command_id else None,
        )
        state = RunState(
            datasource.run_tx_step(
                {"name": COMMIT_STEP_NAME},
                lambda: (
                    commit_resolution(
                        datasource.sql_session(),
                        logical_key,
                        revision_hash,
                        resolved,
                        command,
                    ).value
                ),
            )
        )
        if state is RunState.STARTED:
            schedule_confirmed_effect_continuation(
                durable_action_continuation,
                LogicalEffectKey(logical_key),
                WorkflowRevisionHash(revision_hash),
            )
        return state.value

    @DBOS.workflow(name=ACTION_CONTINUATION_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_action_continuation(logical_key: str, revision_hash: str) -> str:
        typed_key = LogicalEffectKey(logical_key)
        typed_revision = WorkflowRevisionHash(revision_hash)
        run_id, head, round_ordinal, state = checkpoint_confirmed_effect(
            datasource, typed_key, typed_revision
        )
        if RunState(state) is RunState.STARTED:
            start_node(RunId(run_id), typed_revision, head, round_ordinal)
        return state

    @DBOS.workflow(name=ANSWER_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_answer(
        run_id: str,
        revision_hash: str,
        node_id: str,
        round_ordinal: int = FIRST_ROUND_ORDINAL,
    ) -> str:
        """Apply the answer one execution of one waiting node was given.

        The round has a default because an answer enqueued before a pause could
        stand in a round named only the node, and the one round such a run could
        have paused in is the first.
        """
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)

        def apply() -> list[str]:
            answer = load_wait_answer(
                datasource.sql_session(),
                typed_run_id,
                typed_revision,
                node_id,
                round_ordinal,
            ).answer
            transition = commit_wait_answered(datasource.sql_session(), answer)
            return [transition.current_node_id, transition.state.value]

        # The step reports the state as well as the head, because an answered Wait
        # node that is its run's sink ends the run, and after a terminal transition
        # correctness must not rest on DBOS deduplicating a workflow id: starting
        # the sink's own node again is harmless only for as long as that id happens
        # to collide. The state is what says "there is nothing left to start", so
        # the decision is read from the run rather than borrowed from the runtime.
        # Recording two values is why the step name carries a version -- a
        # recovered step of the earlier shape would be read as a head alone.
        head, state = cast(
            tuple[str, str],
            tuple(datasource.run_tx_step({"name": ANSWER_COMMIT_STEP_NAME}, apply)),
        )
        # The heir starts in the round the answered pause stood in. That is the
        # exact round while a Wait may not stand inside a loop body, because
        # every node outside a loop stands in the first round. #658 P3 legalises
        # one there and owes this step a target round of its own: that is a
        # change to a recorded step's return shape, not to an argument, so it
        # cannot be carried by a default the way this workflow's round is.
        if RunState(state) is RunState.STARTED:
            start_node(typed_run_id, typed_revision, head, round_ordinal)
        return state
