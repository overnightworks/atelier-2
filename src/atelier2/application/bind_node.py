"""What a node of a started run binds, and the request that carries it to a provider.

**Why this exists.** Both decisions lived in `adapters/dbos/workflow.py`: five
branches over a durable dictionary decided what a node meant, and a sixth built
the agent request out of the same dictionary. Neither is a durability concern --
they read a document and a run and answer with values -- and while they sat in
the adapter, no test could ask them anything without a database behind it.

**The seam this module is.** The adapter loads, serializes and enqueues; it asks
here what a node binds and what request that binding makes. Nothing in this
module reaches a store, a queue or a clock: everything it reads is handed to it,
including the material a V3 node was given and the source its runtime pinned.
"""

from __future__ import annotations

from atelier2.application.compose_node_job import node_job
from atelier2.contracts.agent_modes import AgentModeMismatch, node_mode_mismatch
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.node_bindings import (
    ActionNodeBinding,
    AgentNodeBindingV2,
    NodeBinding,
    WaitNodeBinding,
)
from atelier2.contracts.node_records_v3 import DeliveredOutput, RunInput
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.contracts.run_bindings import AnyRun, RunBindingConflict, RunV2, RunV3
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocumentNode,
    WaitNodeV3,
)
from atelier2.ports.project_verification import DeclaredProject, PinnedProjectSource


def require_the_run_stands_on(
    run: AnyRun, revision_hash: WorkflowRevisionHash, node_id: str
) -> None:
    """Refuse unless this run stands on this node right now, reading nothing else.

    It answers from the run row alone so that it can answer first: a node the run
    has already left must be refused before its graph, its material or its
    project head is resolved, because none of that will ever reach a binding.
    """
    if (
        run.revision_hash != revision_hash
        or run.current_node_id != node_id
        or run.state is not RunState.STARTED
    ):
        raise RunBindingConflict("node workflow does not own current STARTED node")


def bind_node(
    run: AnyRun,
    node: AnyWorkflowDocumentNode,
    *,
    orders: tuple[RunInput, ...] = (),
    results: tuple[DeliveredOutput, ...] = (),
    tool_grant: DeclaredToolGrant | None = None,
    project_source: ProjectSourcePin | None = None,
    declared_output_schema_document: str | None = None,
    maximum_assistant_turns: int | None = None,
    start_candidate: CandidateTree | None = None,
) -> NodeBinding:
    """What this node binds: its form, and the material that form carries.

    `orders` and `results` are what a V3 Agent or Wait node declared it reads,
    already fetched; the other forms are given none and read none.
    `project_source` is the pin the runtime took for this binding -- taken once
    here rather than at launch, so a commit landing in between cannot change what
    a started run works on. `start_candidate` is the published work this node
    declared it goes on in, read once here for the same reason.
    """
    if isinstance(node, AgentNodeV3):
        if not isinstance(run, (RunV2, RunV3)):
            raise RunBindingConflict("Agent node belongs to a V1 run")
        resolved = next(
            (
                binding
                for binding in run.agent_bindings
                if binding.role.value == node.role
            ),
            None,
        )
        if resolved is None:
            raise RunBindingConflict("V2 Agent role has no durable binding")
        job = node_job(node.instruction, orders, results)
        return AgentNodeBindingV2(
            resolved,
            job,
            tool_grant,
            project_source,
            declared_output_schema_document,
            run.current_round_ordinal,
            maximum_assistant_turns,
            start_candidate,
        )
    if isinstance(node, ActionNodeV3):
        return ActionNodeBinding()
    if isinstance(node, WaitNodeV3):
        question = node_job(node.prompt, orders, results) if node.inputs else None
        return WaitNodeBinding(run.current_round_ordinal, question)
    raise AssertionError("closed WorkflowNode union was not exhaustive")


def agent_execution_request_v2(
    binding: AgentNodeBindingV2,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    operational_identity: AgentExecutorOperationalIdentity,
    declared_capabilities: frozenset[AgentExecutionCapability],
) -> AgentExecutionRequestV2:
    """The V2 request this binding makes, under an executor that attests it can run it.

    The attestation is the caller's, not the binding's: a durable binding may ask
    for a capability the host that recovered it does not offer, and running it
    headless because that host is what recovered it would silently execute
    something other than what was bound.
    """
    if binding.resolved.configuration.requested_capability not in declared_capabilities:
        raise RunBindingConflict(
            "runtime executor lacks the durably requested capability"
        )
    try:
        return AgentExecutionRequestV2(
            NodeExecutionId.for_node(
                run_id, revision_hash, node_id, binding.round_ordinal
            ),
            run_id,
            revision_hash,
            node_id,
            binding.resolved,
            operational_identity,
            binding.job.encode("utf-8"),
            _published_schema_bytes(binding),
            binding.round_ordinal,
            binding.maximum_assistant_turns,
        )
    except (TypeError, ValueError) as error:
        raise RunBindingConflict(
            "V2 agent request contract carries an invalid combination"
        ) from error


def _published_schema_bytes(binding: AgentNodeBindingV2) -> bytes | None:
    """The schema document as the bytes it was published as, or nothing."""
    document = binding.declared_output_schema_document
    return None if document is None else document.encode("utf-8")


def pinned_project(
    binding: AgentNodeBindingV2, project: DeclaredProject | None
) -> PinnedProjectSource | None:
    """The project this attempt works in, or the named refusal that none does."""
    if binding.project_source is None:
        return None
    if project is None:
        raise RunBindingConflict(
            "a node whose binding pinned a project source requires the declared "
            "project, and this runtime was given none"
        )
    return project.pinned(
        binding.project_source, binding.tool_grant, binding.start_candidate
    )


def bound_outside_its_mode(
    binding: AgentNodeBindingV2, node: AgentNodeV3
) -> AgentModeMismatch | None:
    """Whether this durable binding would run its node in a mode it never declared.

    The start refuses such a binding before its run exists, but an attempt
    replays the binding its run recorded, and a run recorded before that check
    carries it still; so every attempt asks again before anything of it is
    written.
    """
    return node_mode_mismatch(node, binding.resolved.configuration.requested_capability)
