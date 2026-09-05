from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_effect_grants import (
    agent_node_redeems_platform_effect,
    open_pr_capability_for,
    push_atelier_commit_capability_for,
)
from atelier2.adapters.dbos.agent_effect_grants import (
    read_pinned_effect_tool_grant as read_agent_pinned_effect_tool_grant,
)
from atelier2.adapters.dbos.agent_effect_grants import (
    read_pinned_exec_tool_grant as read_agent_pinned_exec_tool_grant,
)
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    encode_readback,
    fork_fenced_resolution,
    intent_snapshot_from_record,
    load_intent,
    receipt_from_record,
)
from atelier2.adapters.dbos.run_store import load_node_output_payload
from atelier2.adapters.dbos.run_transitions import load_graph, load_run
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    attempt_instants,
    effect_intents,
    effect_receipts,
    published_revisions,
    run_events,
    run_inputs_v3,
    runs,
)
from atelier2.adapters.dbos.work_item_intents import (
    head_branch_for_work_item,
    issue_work_item_order,
    open_pr_work_item_reference,
)
from atelier2.adapters.yaml_workflows import WorkflowFormatNotExecutable
from atelier2.contracts.adapter_operations_v3 import (
    AdapterOperationAccepted,
    AdapterOperationName,
    read_adapter_operation_document,
)
from atelier2.contracts.effect_requests import (
    HeadBranch,
    OpenPullRequest,
    PushAtelierCommit,
    PushAtelierCommitReceipt,
    ReviewedDocumentationPullRequest,
    ReviewedDocumentReplacement,
    head_branch_for_unbound_request,
    reviewed_documentation_candidate_digest,
)
from atelier2.contracts.effects import (
    CanonicalRequest,
    EffectAdapterBinding,
    EffectBinding,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant
from atelier2.contracts.work_items import WorkItemOrderDocument
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows import producing_round
from atelier2.contracts.workflows_v3 import (
    DOCUMENTATION_RELEASE_ACTION_INPUT_NAMES,
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocumentNode,
    GraphInputSource,
    action_body_source,
    is_documentation_release_action_form,
)
from atelier2.ports.agent_tool_effects import (
    AgentToolEffectPending,
    redeem_prepared_tool_effect,
)
from atelier2.ports.effects import EffectAdapter


class EffectIntentIdentityConflict(RuntimeError):
    """A logical effect key was retried with different immutable input."""


class RunEffectConflict(RuntimeError):
    """A V1 run cannot prepare this effect against its durable run binding."""


def graph_action_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None = None,
) -> EffectIntent:
    run = load_run(session, run_id)
    graph = load_graph(session, revision_hash)
    action = graph.node(run.current_node_id)
    if (
        run.revision_hash != revision_hash
        or run.state is not RunState.STARTED
        or not isinstance(action, ActionNodeV3)
    ):
        raise RunEffectConflict("effect requires the current STARTED Action")
    if is_documentation_release_action_form(action):
        return _documentation_release_action_intent(
            session,
            run_id,
            revision_hash,
            action,
            effect_adapter_bindings,
            project_id,
        )
    body_source = action_body_source(action)
    if body_source is None:
        raise RunEffectConflict("Action declares no bound input form")
    predecessor = graph.node(body_source.node)
    if not isinstance(predecessor, AgentNodeV3):
        raise RunEffectConflict("Action body input names no Agent output")
    producing = producing_round(
        graph, action.id, predecessor.id, run.current_round_ordinal
    )
    if producing is None:
        raise RunEffectConflict("Action body input names an output not yet written")
    payload = load_node_output_payload(
        session,
        run_id,
        revision_hash,
        graph,
        predecessor.id,
        producing,
    )
    operation = _operation_for(session, action.operation)
    effect_adapter_binding = _binding_for(effect_adapter_bindings, operation.operation)
    request = CanonicalRequest(payload)
    if operation.operation is AdapterOperationName.OPEN_PR:
        work_item = None
        if project_id is not None:
            head_branch = _confirmed_push_branch(
                session,
                run_id,
                revision_hash,
                predecessor,
                producing,
                project_id,
            )
            work_item = issue_work_item_order(session, run_id)
        else:
            head_branch = head_branch_for_unbound_request(payload)
        request = _open_pr_request(payload, head_branch, work_item, "Action")
    binding = EffectBinding(
        logical_effect_key_for_node(
            run_id, revision_hash, action.id, run.current_round_ordinal
        ),
        run_id,
        revision_hash,
        effect_adapter_binding.adapter_revision,
        effect_adapter_binding.destination,
        effect_adapter_binding.operational_identity,
        operation.operation,
    )
    return EffectIntent(binding, request)


def _documentation_release_action_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    action: ActionNodeV3,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None,
) -> EffectIntent:
    """Bind the independently reviewed release order at the effect boundary."""
    if project_id is None:
        raise RunEffectConflict("documentation release requires its project binding")
    operation = _operation_for(session, action.operation)
    if operation.operation is not AdapterOperationName.OPEN_PR:
        raise RunEffectConflict("documentation release Action pins open-pr")
    orders = _documentation_release_orders(session, run_id, action)
    candidate = _object_order(orders["candidate"], "documentation candidate")
    verdict_bytes = orders["approved_verdict"]
    verdict = _object_order(verdict_bytes, "documentation trace-review verdict")
    changes = candidate.get("changes")
    if not isinstance(changes, list):
        raise RunEffectConflict("documentation candidate carries no reviewed changes")
    try:
        candidate_digest = _text_field(
            candidate, "candidate_digest", "documentation candidate"
        )
        base_revision = _text_field(
            candidate, "base_revision", "documentation candidate"
        )
        replacements = tuple(
            ReviewedDocumentReplacement(
                _text_field(change, "path", "documentation change"),
                _text_field(change, "current_digest", "documentation change"),
                _text_field(
                    change, "replacement_utf8_content", "documentation change"
                ).encode("utf-8"),
            )
            for change in changes
        )
        title = _text_field(candidate, "title", "documentation candidate")
        body = _text_field(candidate, "body", "documentation candidate")
        bound_candidate_digest = reviewed_documentation_candidate_digest(
            base_revision, replacements, title, body
        )
        if (
            verdict.get("verdict") != "approve"
            or verdict.get("candidate_digest") != candidate_digest
            or candidate_digest != bound_candidate_digest
        ):
            raise RunEffectConflict(
                "documentation release requires an approved verdict for its "
                "exact candidate"
            )
        request = ReviewedDocumentationPullRequest(
            base_revision,
            candidate_digest,
            Sha256Hash.of(verdict_bytes).value,
            replacements,
            title,
            body,
            _head_branch(session, run_id, project_id),
        )
    except RunEffectConflict:
        raise
    except (TypeError, ValueError) as error:
        raise RunEffectConflict("documentation release request is invalid") from error
    owner = _binding_for(effect_adapter_bindings, operation.operation)
    binding = EffectBinding(
        logical_effect_key_for_node(
            run_id,
            revision_hash,
            action.id,
            load_run(session, run_id).current_round_ordinal,
        ),
        run_id,
        revision_hash,
        owner.adapter_revision,
        owner.destination,
        owner.operational_identity,
        operation.operation,
    )
    return EffectIntent(binding, CanonicalRequest(request.canonical_bytes()))


def _documentation_release_orders(
    session: Any, run_id: RunId, action: ActionNodeV3
) -> dict[str, bytes]:
    sources = {
        entry.name: entry.source.graph_input
        for entry in action.inputs
        if isinstance(entry.source, GraphInputSource)
    }
    if set(sources) != DOCUMENTATION_RELEASE_ACTION_INPUT_NAMES:
        raise RunEffectConflict(
            "documentation release Action declares work_item, candidate and "
            "approved_verdict"
        )
    records = session.execute(
        sa.select(run_inputs_v3.c.name, run_inputs_v3.c.value).where(
            run_inputs_v3.c.run_id == run_id.value,
            run_inputs_v3.c.name.in_(sources.values()),
        )
    ).all()
    stored = {str(record.name): bytes(record.value) for record in records}
    if set(stored) != set(sources.values()):
        raise RunEffectConflict("documentation release has a missing declared order")
    return {name: stored[sources[name]] for name in ("candidate", "approved_verdict")}


def _object_order(value: bytes, owner: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunEffectConflict(f"{owner} is not JSON") from error
    if not isinstance(decoded, dict):
        raise RunEffectConflict(f"{owner} is not an object")
    return decoded


def _text_field(value: object, field: str, owner: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise TypeError(f"{owner} has no text {field}")
    return str(value[field])


def prepare_graph_action(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None = None,
) -> EffectIntentSnapshot:
    intent = graph_action_intent(
        session, run_id, revision_hash, effect_adapter_bindings, project_id
    )
    return prepared_effect_intent(session, intent)


def prepared_effect_intent(session: Any, intent: EffectIntent) -> EffectIntentSnapshot:
    """Record this intent PREPARED, or return the one already durably prepared.

    The logical key is the effect's durable identity: a retry that derives the
    same key from the same immutable node must find the same intent, and a key
    that already belongs to a different intent is a contradiction rather than a
    second preparation. Recording it before any adapter is asked is what lets a
    redemption that never reaches its destination leave a named PREPARED intent
    behind instead of an effect nobody durably asked for.
    """
    existing_record = (
        session.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == intent.binding.logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing_record is not None:
        snapshot = intent_snapshot_from_record(existing_record)
        if snapshot.intent != intent:
            raise EffectIntentIdentityConflict(
                "logical effect key already belongs to another exact intent"
            )
        return snapshot
    session.execute(
        effect_intents.insert().values(
            logical_key=intent.binding.logical_key.value,
            run_id=intent.binding.run_id.value,
            canonical_request=intent.request.payload,
            request_hash=intent.request.request_hash.value,
            workflow_revision_hash=intent.binding.workflow_revision_hash.value,
            adapter_revision=intent.binding.adapter_revision.value,
            destination_identity=intent.binding.destination.value,
            adapter_operational_identity=(
                intent.binding.adapter_operational_identity.value
            ),
            operation_name=intent.binding.operation_name.value,
            state=EffectIntentState.PREPARED.value,
            state_version=0,
            reconciliation_owner_command_id=None,
        )
    )
    return EffectIntentSnapshot(
        intent,
        EffectIntentState.PREPARED,
        EffectIntentStateVersion(0),
    )


def read_pinned_exec_tool_grant(
    session: Any, node: AnyWorkflowDocumentNode
) -> DeclaredToolGrant | None:
    """The exec-shaped grant this node pinned, read from the revision it names.

    A V3 `tools` entry pins its published revision by that revision's own hash,
    so reading the registry under it is reading exactly what the run
    configuration froze rather than resolving a second time. The bytes are
    immutable and were already read as a grant when the run was bound; a
    registry that cannot answer for them now contradicts a run that has already
    started. This is the binding's own read -- an exec-shaped grant is carried
    in the durable binding and redeemed inside the attempt's own lease.
    """
    if not isinstance(node, AgentNodeV3):
        return None
    return read_agent_pinned_exec_tool_grant(session, node)


def read_pinned_effect_tool_grant(
    session: Any, node: AnyWorkflowDocumentNode
) -> DeclaredToolGrant | None:
    """The effect-shaped grant this node pinned, read from the revision it names.

    Same door as `read_pinned_exec_tool_grant`, for the other shape: an
    effect-shaped grant carries no `project_source` and is read straight from
    the immutable graph where its effect is prepared, after the attempt has
    already succeeded.
    """
    if not isinstance(node, AgentNodeV3):
        return None
    return read_agent_pinned_effect_tool_grant(session, node)


def legacy_agent_effect_runs_without_receipt(engine: sa.Engine) -> tuple[RunId, ...]:
    """Find persisted pre-reconciliation agent-effect checkpoints.

    Before agent effects entered the shared continuation, an agent could advance
    its run and then redeem its grant. Current runs remain on that agent until
    a receipt exists, or wait there for reconciliation. A completed or advanced
    agent execution without the receipt that now authorizes its advance is
    therefore a persisted pre-change shape, not recoverable current work.

    A run's revision can also carry a shape this build's parser no longer
    executes at all -- persisted before a later format tightening retired it,
    the same tightening `parse_executable_workflow_document` enforces at start
    (`DurableRunFormatNotExecutable`). That revision is not one this sweep can
    read at all, so it is named retired-shape and skipped rather than let its
    parse refusal abort the whole sweep and take live serving down with it.
    """

    logger = logging.getLogger("atelier2")
    with engine.connect() as connection:
        records = connection.execute(
            sa.select(runs).where(
                runs.c.workflow_format_version == int(WorkflowFormatVersion.V3)
            )
        ).mappings()
        blocking: set[RunId] = set()
        for record in records:
            run_id = RunId(str(record["run_id"]))
            revision_hash = WorkflowRevisionHash(str(record["revision_hash"]))
            try:
                graph = load_graph(connection, revision_hash)
            except WorkflowFormatNotExecutable:
                logger.warning(
                    "skipping retired-shape workflow revision %s for run %s: no "
                    "runtime executes this format any more",
                    revision_hash.value,
                    run_id.value,
                )
                continue
            current_node_id = str(record["current_node_id"])
            current_round_ordinal = int(record["current_round_ordinal"])
            current_node = graph.node(current_node_id)
            if (
                isinstance(current_node, AgentNodeV3)
                and agent_node_redeems_platform_effect(connection, current_node)
                and RunState(str(record["state"])) is RunState.COMPLETED
                and not _effect_receipt_exists(
                    connection,
                    logical_effect_key_for_node(
                        run_id,
                        revision_hash,
                        current_node_id,
                        current_round_ordinal,
                    ).value,
                )
            ):
                blocking.add(run_id)
                continue
            for event in connection.execute(
                sa.select(run_events.c.node_id, run_events.c.round_ordinal).where(
                    run_events.c.run_id == run_id.value,
                    run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
                )
            ).mappings():
                node_id = str(event["node_id"])
                round_ordinal = int(event["round_ordinal"])
                node = graph.node(node_id)
                if (
                    not isinstance(node, AgentNodeV3)
                    or not agent_node_redeems_platform_effect(connection, node)
                    or _effect_receipt_exists(
                        connection,
                        logical_effect_key_for_node(
                            run_id, revision_hash, node_id, round_ordinal
                        ).value,
                    )
                    or (
                        current_node_id == node_id
                        and current_round_ordinal == round_ordinal
                    )
                ):
                    continue
                blocking.add(run_id)
        return tuple(sorted(blocking, key=lambda run: run.value))


def _effect_receipt_exists(connection: Any, logical_key: str) -> bool:
    return (
        connection.scalar(
            sa.select(effect_receipts.c.logical_key).where(
                effect_receipts.c.logical_key == logical_key
            )
        )
        is not None
    )


def graph_agent_open_pr_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_binding: EffectAdapterBinding,
    project_id: ProjectId | None = None,
) -> EffectIntent | None:
    """The pull-request this agent node's own grant opens, or nothing where none does.

    A node with no grant, an exec-shaped grant, or the push grant handled by the
    sibling preparation creates no open-PR intent here. A future effect-shaped
    capability still fails loud in the shared grant classifier.

    The request bytes are the node's own durable receipt output rather than
    trusted from memory, because the same provider bytes the run kept are what
    the pull request must carry. The
    effect binds to the connected repo the adapter names -- not to a
    `project_source` tree-pin -- because a pull request targets a repository,
    not a tree state, so an `open-pr` grant needs no pinned source at all. The
    logical key is derived from this node's own execution identity, which makes
    it deterministic, collision-free, and distinct from the key an Action of the
    same operation would derive from its own node.
    """
    grant = read_pinned_effect_tool_grant(
        session, load_graph(session, revision_hash).node(node_id)
    )
    if open_pr_capability_for(grant) is None:
        return None
    execution_id = NodeExecutionId.for_node(
        run_id, revision_hash, node_id, round_ordinal
    )
    binding = EffectBinding(
        logical_effect_key_for_node(run_id, revision_hash, node_id, round_ordinal),
        run_id,
        revision_hash,
        effect_adapter_binding.adapter_revision,
        effect_adapter_binding.destination,
        effect_adapter_binding.operational_identity,
        AdapterOperationName.OPEN_PR,
    )
    payload = _agent_output(session, execution_id)
    if project_id is None:
        work_item = None
        head_branch = head_branch_for_unbound_request(payload)
    else:
        work_item = issue_work_item_order(session, run_id)
        head_branch = head_branch_for_work_item(work_item, project_id)
    return EffectIntent(
        binding, _open_pr_request(payload, head_branch, work_item, "Agent")
    )


def prepare_graph_agent_open_pr(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None = None,
) -> str | None:
    """Prepare this agent node's own pull-request intent, or nothing where none is.

    The logical key it returns is what the redemption step then loads: the two
    are separate durable steps so a PREPARED intent is committed before any
    adapter is asked, exactly as the effect-shaped redemption port requires.
    """
    intent = graph_agent_open_pr_intent(
        session,
        run_id,
        revision_hash,
        node_id,
        round_ordinal,
        _binding_for(effect_adapter_bindings, AdapterOperationName.OPEN_PR),
        project_id,
    )
    if intent is None:
        return None
    return prepared_effect_intent(session, intent).intent.binding.logical_key.value


def prepare_graph_agent_push(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    attempt_id: str,
    candidate_tree: str,
    base_commit: str,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId,
) -> str | None:
    """Prepare the exact candidate publication earned by a pinned push grant."""

    node = load_graph(session, revision_hash).node(node_id)
    grant = read_pinned_effect_tool_grant(session, node)
    if push_atelier_commit_capability_for(grant) is None:
        return None
    assert grant is not None and grant.operation is not None
    operation = _operation_for(session, grant.operation)
    if (
        operation.operation is not AdapterOperationName.PUSH_ATELIER_COMMIT
        or operation.author is None
        or operation.committer is None
    ):
        raise RunEffectConflict(
            "pinned push grant does not resolve to a push operation"
        )
    binding_owner = _binding_for(effect_adapter_bindings, operation.operation)
    completed_at = session.scalar(
        sa.select(attempt_instants.c.ended_at).where(
            attempt_instants.c.attempt_id == attempt_id
        )
    )
    if not isinstance(completed_at, str):
        raise RunEffectConflict("successful push attempt has no completion instant")
    request = PushAtelierCommit(
        attempt_id,
        candidate_tree,
        base_commit,
        _head_branch(session, run_id, project_id),
        operation.author,
        operation.committer,
        completed_at,
    )
    binding = EffectBinding(
        logical_effect_key_for_node(run_id, revision_hash, node_id, round_ordinal),
        run_id,
        revision_hash,
        binding_owner.adapter_revision,
        binding_owner.destination,
        binding_owner.operational_identity,
        operation.operation,
    )
    return prepared_effect_intent(
        session, EffectIntent(binding, CanonicalRequest(request.canonical_bytes()))
    ).intent.binding.logical_key.value


def redeem_agent_effect(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
) -> str:
    """Redeem one PREPARED agent effect intent through its selected adapter.

    Readback runs before create, so a redemption retried after the pull request
    already exists is recognized rather than opened twice. The receipt reaches
    the same `effect_receipts` row an Action's confirmation writes, through the
    same `commit_resolution`, so what an operator reads back is one effect
    whichever authorization opened it. An `UNKNOWN` readback instead durably
    moves the run and its intent to reconciliation; it never guesses or lets
    the agent complete before a receipt exists.
    """
    fenced = fork_fenced_resolution(session, logical_key, revision_hash)
    if fenced is not None:
        return commit_resolution(session, logical_key, revision_hash, fenced).value
    intent = load_intent(session, logical_key, revision_hash)
    redemption = redeem_prepared_tool_effect(intent, adapter)
    if isinstance(redemption, AgentToolEffectPending):
        return commit_resolution(
            session,
            logical_key,
            revision_hash,
            encode_readback(redemption.unknown),
        ).value
    return commit_resolution(
        session, logical_key, revision_hash, encode_readback(redemption.receipt)
    ).value


def _agent_output(session: Any, execution_id: NodeExecutionId) -> bytes:
    record = session.execute(
        sa.select(
            agent_receipts_v2.c.output_bytes, agent_receipts_v2.c.output_hash
        ).where(
            agent_receipts_v2.c.node_execution_id == execution_id.value,
        )
    ).one_or_none()
    if record is None:
        raise RunEffectConflict("agent effect grant has no durable agent receipt")
    payload = bytes(record.output_bytes)
    if Sha256Hash.of(payload).value != record.output_hash:
        raise RunEffectConflict("agent output binding changed")
    return payload


def _confirmed_push_branch(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    predecessor: AgentNodeV3,
    round_ordinal: int,
    project_id: ProjectId,
) -> HeadBranch:
    logical_key = logical_effect_key_for_node(
        run_id, revision_hash, predecessor.id, round_ordinal
    )
    record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunEffectConflict(
            "project open-pr Action requires its predecessor's confirmed push receipt"
        )
    try:
        receipt = receipt_from_record(record)
        request = PushAtelierCommit.from_canonical_bytes(receipt.intent.request.payload)
        result = PushAtelierCommitReceipt.from_result_bytes(receipt.result.payload)
        result_branch = HeadBranch(result.branch)
    except (TypeError, ValueError) as error:
        raise RunEffectConflict("confirmed push receipt is corrupt") from error
    expected_commit = request.expected_commit_oid(
        receipt.intent.request.request_hash.value
    )
    if (
        receipt.intent.binding.operation_name
        is not AdapterOperationName.PUSH_ATELIER_COMMIT
        or receipt.intent.binding.run_id != run_id
        or receipt.intent.binding.workflow_revision_hash != revision_hash
        or result.remote_identity
        != receipt.intent.binding.adapter_operational_identity.value
        or result.commit_oid != receipt.effect_id.value
        or result.commit_oid != expected_commit
        or result.full_ref != request.head_branch.full_ref
        or result.parent != request.base_commit
        or result.candidate_tree != request.candidate_tree
        or result_branch != request.head_branch
        or result.author != request.author
        or result.committer != request.committer
        or result_branch != _head_branch(session, run_id, project_id)
    ):
        raise RunEffectConflict(
            "confirmed push receipt disagrees with the open-pr head"
        )
    return result_branch


def _operation_for(session: Any, reference: Any) -> AdapterOperationAccepted:
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.ADAPTER_OPERATION.value,
            published_revisions.c.revision_hash == reference.revision,
        )
    )
    if document is None:
        raise RunEffectConflict("pinned adapter operation left the registry")
    verdict = read_adapter_operation_document(bytes(document))
    if not isinstance(verdict, AdapterOperationAccepted):
        raise RunEffectConflict("pinned adapter operation is corrupt")
    return verdict


def _binding_for(
    bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    operation: AdapterOperationName,
) -> EffectAdapterBinding:
    candidates = (bindings,) if isinstance(bindings, EffectAdapterBinding) else bindings
    matching = tuple(
        binding for binding in candidates if binding.operation_name is operation
    )
    if len(matching) != 1:
        raise RunEffectConflict(
            f"operation {operation.value!r} does not have exactly one adapter"
        )
    return matching[0]


def _head_branch(session: Any, run_id: RunId, project_id: ProjectId) -> HeadBranch:
    return head_branch_for_work_item(issue_work_item_order(session, run_id), project_id)


def _open_pr_request(
    payload: bytes,
    head_branch: HeadBranch,
    work_item: WorkItemOrderDocument | None,
    owner: str,
) -> CanonicalRequest:
    """The canonical open-pr request this node's decoded output and head carry.

    Shared by the Action- and Agent-shaped open-pr builders, which differ only
    in whose output they decode and how they name it in the UTF-8 conflict.
    """
    try:
        body = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunEffectConflict(f"open-pr {owner} output is not UTF-8") from error
    return CanonicalRequest(
        OpenPullRequest(
            body, head_branch, open_pr_work_item_reference(work_item)
        ).canonical_bytes()
    )
