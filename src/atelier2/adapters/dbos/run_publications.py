"""What a run has already published, and where its next publisher stands.

A node with a push grant does not begin on whatever the project's head happens
to be. The first publisher of a run does -- nothing of that run stands on the
item's branch yet -- but every later one continues the publication that already
does: it takes that publication's base, so its own commit keeps the same parent
and replaces the branch head without dropping what trunk gained beside it. A
publisher that is tried a second time reads its own publication back the same
way, so a replacement pins what the original pinned rather than a head that has
moved since.

Which publication is the last one is answered by the workflow's declared order
and not by when a receipt happened to be written: two publications the graph
never ordered against each other are refused by name rather than guessed
between, because picking either would decide by accident what a run stands on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_effect_grants import (
    push_atelier_commit_capability_for,
    read_pinned_effect_tool_grant,
)
from atelier2.adapters.dbos.effect_store import receipt_from_record
from atelier2.adapters.dbos.schema import effect_receipts
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    HeadBranch,
    PushAtelierCommit,
    PushAtelierCommitReceipt,
)
from atelier2.contracts.effects import EffectReceipt
from atelier2.contracts.executions import logical_effect_key_for_node
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflows import round_of
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    WorkflowGraphV3,
    WorkflowNodeV3,
)
from atelier2.ports.project_source import ProjectSourceRepository


class RunPublicationRefused(RuntimeError):
    """A run's confirmed publications cannot answer, so nothing is pinned or opened."""


_MISSING_FOR_OPEN_PR = (
    "project open-pr Action requires its predecessor's confirmed push receipt"
)
DISAGREES_WITH_OPEN_PR_HEAD = "confirmed push receipt disagrees with the open-pr head"
_DISAGREES_WITH_ITS_PUSH = "confirmed push receipt disagrees with the push it reports"


@dataclass(frozen=True, slots=True)
class NodeInRun:
    """One node's execution: which run, which revision of it, and which round.

    The round is the one the run stands in. A reader asking about another node
    derives that node's own round from the graph instead, because a node no loop
    repeats runs exactly once whatever round its run has reached.
    """

    run_id: RunId
    revision_hash: WorkflowRevisionHash
    node_id: str
    round_ordinal: int


@dataclass(frozen=True, slots=True)
class RunPublication:
    """One confirmed push of a run: what it stood on and what it moved."""

    branch: HeadBranch
    base_commit: str
    candidate_tree: str


def confirmed_publication(session: Any, node: NodeInRun) -> RunPublication:
    """The publication this exact node confirmed, for the open-pr standing on it.

    Its refusals name that open-pr, because the head a pull request would be
    opened over is what the reader of such a refusal is holding.
    """
    record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == _logical_key(node)
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunPublicationRefused(_MISSING_FOR_OPEN_PR)
    return _publication_from(record, node, DISAGREES_WITH_OPEN_PR_HEAD)


def pinned_source_for(
    session: Any,
    graph: WorkflowGraphV3,
    node: NodeInRun,
    source: ProjectSourceRepository,
) -> ProjectSourcePin:
    """The commit and tree this node's attempt stands on.

    A publisher continuing a publication of its own run stands where that
    publication stood; everyone else -- a first publisher, a node that publishes
    nothing -- stands on the head.
    """
    publication = _last_publication(session, graph, node)
    if publication is None:
        return source.head()
    return source.pin_at(publication.base_commit)


def last_in_workflow_order(
    graph: WorkflowGraphV3, published: Mapping[str, RunPublication]
) -> RunPublication:
    """The one publication every other stands before, or a refusal to guess.

    Refused rather than resolved by receipt time: two publications the workflow
    never ordered against each other name no last one, and picking either would
    decide by accident which tree the run goes on standing on.
    """
    for node_id, publication in published.items():
        if set(published) - {node_id} <= graph.dependency_closure(node_id):
            return publication
    raise RunPublicationRefused(
        "the workflow orders none of the confirmed publications of "
        f"{', '.join(sorted(published))} after the others, so which one a later "
        "publisher continues is not this run's to choose"
    )


def _last_publication(
    session: Any, graph: WorkflowGraphV3, node: NodeInRun
) -> RunPublication | None:
    publishers = _publishers_up_to(session, graph, node.node_id)
    if node.node_id not in publishers:
        return None
    published = _confirmed_publications(session, graph, node, publishers)
    if not published:
        return None
    return last_in_workflow_order(graph, published)


def _publishers_up_to(
    session: Any, graph: WorkflowGraphV3, node_id: str
) -> frozenset[str]:
    """Every node holding a push grant that this node is, or that stands before it."""
    ordered_at_or_before = graph.dependency_closure(node_id) | {node_id}
    return frozenset(
        candidate.id
        for candidate in graph.nodes
        if candidate.id in ordered_at_or_before and _publishes(session, candidate)
    )


def _publishes(session: Any, node: WorkflowNodeV3) -> bool:
    if not isinstance(node, AgentNodeV3):
        return False
    grant = read_pinned_effect_tool_grant(session, node)
    return push_atelier_commit_capability_for(grant) is not None


def _confirmed_publications(
    session: Any,
    graph: WorkflowGraphV3,
    node: NodeInRun,
    publishers: frozenset[str],
) -> dict[str, RunPublication]:
    """What each of these publishers has already confirmed, by the node that did.

    A publisher whose push is not confirmed yet has published nothing and is
    absent here; only a receipt that exists and disagrees with itself refuses.
    """
    publisher_of_key = {
        _logical_key(_execution_of(graph, node, publisher)): publisher
        for publisher in publishers
    }
    records = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key.in_(publisher_of_key)
            )
        )
        .mappings()
        .all()
    )
    published: dict[str, RunPublication] = {}
    for record in records:
        publisher = publisher_of_key[str(record["logical_key"])]
        published[publisher] = _publication_from(
            record, _execution_of(graph, node, publisher), _DISAGREES_WITH_ITS_PUSH
        )
    return published


def _execution_of(graph: WorkflowGraphV3, node: NodeInRun, node_id: str) -> NodeInRun:
    return NodeInRun(
        node.run_id,
        node.revision_hash,
        node_id,
        round_of(graph, node_id, node.round_ordinal),
    )


def _logical_key(node: NodeInRun) -> str:
    return logical_effect_key_for_node(
        node.run_id, node.revision_hash, node.node_id, node.round_ordinal
    ).value


def _publication_from(
    record: Mapping[Any, Any], node: NodeInRun, disagreement: str
) -> RunPublication:
    """The publication one receipt describes, refused in the caller's own words."""
    try:
        receipt = receipt_from_record(record)
        request = PushAtelierCommit.from_canonical_bytes(receipt.intent.request.payload)
        result = PushAtelierCommitReceipt.from_result_bytes(receipt.result.payload)
        branch = HeadBranch(result.branch)
    except (TypeError, ValueError) as error:
        raise RunPublicationRefused("confirmed push receipt is corrupt") from error
    if _disagrees(receipt, request, result, node):
        raise RunPublicationRefused(disagreement)
    return RunPublication(branch, request.base_commit, request.candidate_tree)


def _disagrees(
    receipt: EffectReceipt,
    request: PushAtelierCommit,
    result: PushAtelierCommitReceipt,
    node: NodeInRun,
) -> bool:
    """Whether this receipt and the request it answers describe two different pushes."""
    expected_commit = request.expected_commit_oid(
        receipt.intent.request.request_hash.value
    )
    return (
        receipt.intent.binding.operation_name
        is not AdapterOperationName.PUSH_ATELIER_COMMIT
        or receipt.intent.binding.run_id != node.run_id
        or receipt.intent.binding.workflow_revision_hash != node.revision_hash
        or result.remote_identity
        != receipt.intent.binding.adapter_operational_identity.value
        or result.commit_oid != receipt.effect_id.value
        or result.commit_oid != expected_commit
        or result.full_ref != request.head_branch.full_ref
        or result.parent != request.base_commit
        or result.candidate_tree != request.candidate_tree
        or result.branch != request.head_branch.value
        or result.author != request.author
        or result.committer != request.committer
    )
