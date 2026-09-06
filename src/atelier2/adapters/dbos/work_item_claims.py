"""The lane claim a node holds on its work item before it may change anything.

Decision 0001's rule for an external effect is the shape here: the intent is
written before the claim command runs, the claim the ledger confirms lands as
that intent's receipt, and a retry reads the ledger back under the same claim
id instead of taking a second claim.

The command is driven through `ports.work_item_claims` rather than through an
`EffectAdapter`, because a refused claim is a decision this node ends on and
the adapter protocol has no word for a destination that refused: it answers
performed or unknown, and an unknown routes to operator reconciliation. The
ledger boundary answers the refusal itself, so the node can end typed and
terminal without a workspace, a provider, or a push.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from atelier2.adapters.dbos.advancer import (
    prepared_effect_intent,
    read_pinned_effect_tool_grant,
)
from atelier2.adapters.dbos.agent_effect_grants import (
    push_atelier_commit_capability_for,
)
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    encode_readback,
    load_intent,
)
from atelier2.adapters.dbos.run_transitions import _commit_event, load_graph
from atelier2.adapters.dbos.work_item_intents import (
    head_branch_for_work_item,
    issue_work_item_order,
)
from atelier2.adapters.github.tracker_reference import github_issue_number_or_none
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    ClaimedLanePath,
    ClaimWorkItem,
    ClaimWorkItemReceipt,
    work_item_claim_id,
)
from atelier2.contracts.effects import (
    CanonicalRequest,
    ConfirmationSource,
    EffectAdapterBinding,
    EffectBinding,
    EffectId,
    EffectIntent,
    EffectReceipt,
    EffectResult,
)
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    RunEventKind,
    logical_effect_key_for_work_item_claim,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.ports.work_item_claims import (
    ClaimAbsent,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    WorkItemClaims,
)

LOGICAL_KEY_FIELD = "logical_key"
REFUSAL_FIELD = "refusal"
UNCONFIGURED_CLAIM_LEDGER = AgentExecutionRefusal.WORK_ITEM_CLAIM_UNCONFIGURED.value
"""The refusal a node ends on where this runtime holds no claim boundary."""

_REFUSAL_WORDS = {
    ClaimRefusalReason.PRIORITY: (
        AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED_BY_PRIORITY
    ),
    ClaimRefusalReason.LEDGER_UNREADABLE: (
        AgentExecutionRefusal.WORK_ITEM_CLAIM_LEDGER_UNREADABLE
    ),
    ClaimRefusalReason.UNKNOWN: AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED,
}


@dataclass(frozen=True, slots=True)
class WorkItemClaimLedger:
    """The claim boundary this runtime holds, and the binding it records with."""

    claims: WorkItemClaims
    binding: EffectAdapterBinding


@dataclass(frozen=True, slots=True)
class ConfirmedWorkItemClaim:
    """The claim the ledger answered with, and how this run learned of it.

    `source` is the effect contract's own provenance: a claim this drive
    posted is `ADAPTER_EXECUTION`, and one it found already standing under
    its claim id is `ADAPTER_READBACK`, so a receipt never says a command
    ran where a read answered.
    """

    receipt: ClaimWorkItemReceipt
    source: ConfirmationSource


@dataclass(frozen=True, slots=True)
class WorkItemClaimHeld:
    """The ledger holds this run's claim, and this is what it confirmed."""

    confirmed: ConfirmedWorkItemClaim


@dataclass(frozen=True, slots=True)
class WorkItemClaimRefused:
    """This node ends here, and `confirmed` is the claim that was still taken.

    A claim the ledger granted over paths another lane already holds is a fact
    that happened: it is recorded as this effect's receipt, and the node ends
    afterwards. Releasing it belongs to the run's own completion.
    """

    reason: AgentExecutionRefusal
    confirmed: ConfirmedWorkItemClaim | None = None


type WorkItemClaimOutcome = WorkItemClaimHeld | WorkItemClaimRefused


def prepare_work_item_claim(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    ledger_binding: EffectAdapterBinding | None,
    project_id: ProjectId | None,
) -> dict[str, str] | None:
    """Record the claim this node owes before any command runs, or name why not.

    Answers nothing where the node changes nothing: only a node whose own
    pinned grant publishes a commit holds its item's claim. Otherwise it
    answers either the prepared intent's logical key or the refusal word this
    node ends on -- a runtime that cannot claim never quietly builds unclaimed.
    """

    node = load_graph(session, revision_hash).node(node_id)
    grant = read_pinned_effect_tool_grant(session, node)
    if push_atelier_commit_capability_for(grant) is None:
        return None
    if ledger_binding is None or project_id is None:
        return {REFUSAL_FIELD: UNCONFIGURED_CLAIM_LEDGER}
    order = issue_work_item_order(session, run_id)
    item = github_issue_number_or_none(order.reference)
    if item is None:
        return {REFUSAL_FIELD: UNCONFIGURED_CLAIM_LEDGER}
    if not order.scope.paths:
        return {REFUSAL_FIELD: AgentExecutionRefusal.WORK_ITEM_NAMES_NO_SCOPE.value}
    request = ClaimWorkItem(
        item,
        work_item_claim_id(run_id, item),
        head_branch_for_work_item(order, project_id),
        order.scope.paths,
    )
    # The claim's own binding is held beside the graph's effect adapters rather
    # than among them: no published document declares this operation, so no
    # graph node can select it.
    binding = EffectBinding(
        logical_effect_key_for_work_item_claim(
            run_id, revision_hash, node_id, round_ordinal
        ),
        run_id,
        revision_hash,
        ledger_binding.adapter_revision,
        ledger_binding.destination,
        ledger_binding.operational_identity,
        AdapterOperationName.CLAIM_WORK_ITEM,
    )
    prepared = prepared_effect_intent(
        session, EffectIntent(binding, CanonicalRequest(request.canonical_bytes()))
    )
    return {LOGICAL_KEY_FIELD: prepared.intent.binding.logical_key.value}


def hold_prepared_claim(
    intent: EffectIntent, ledger: WorkItemClaimLedger
) -> WorkItemClaimOutcome:
    """Take the prepared claim, reading the ledger back before asking for it.

    The claim id is this run's own, so a claim an earlier attempt already
    posted is recognised here instead of being taken twice -- and a ledger that
    answers something other than this exact request is refused rather than
    built on.
    """

    request = ClaimWorkItem.from_canonical_bytes(intent.request.payload)
    scope = tuple(PurePosixPath(path) for path in request.scope)
    standing = ledger.claims.read_back(request.item, request.claim_id)
    source = ConfirmationSource.ADAPTER_READBACK
    if isinstance(standing, ClaimRefusal):
        return WorkItemClaimRefused(_REFUSAL_WORDS[standing.reason])
    if isinstance(standing, ClaimAbsent):
        source = ConfirmationSource.ADAPTER_EXECUTION
        standing = ledger.claims.claim(
            request.item,
            intent.binding.run_id,
            request.head_branch,
            scope,
            request.claim_id,
            None,
        )
        if isinstance(standing, ClaimRefusal):
            return WorkItemClaimRefused(_REFUSAL_WORDS[standing.reason])
    held: ClaimReceipt = standing
    if (
        held.item != request.item
        or held.claim_id != request.claim_id
        or held.branch != request.head_branch
        or held.claimed_scope != scope
    ):
        return WorkItemClaimRefused(AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED)
    confirmed = ConfirmedWorkItemClaim(_confirmed_claim(held), source)
    if held.touches:
        return WorkItemClaimRefused(
            AgentExecutionRefusal.WORK_ITEM_CLAIM_TOUCHES_ANOTHER_LANE, confirmed
        )
    return WorkItemClaimHeld(confirmed)


def confirm_work_item_claim(
    session: Any,
    logical_key: str,
    revision_hash: WorkflowRevisionHash,
    confirmed: ConfirmedWorkItemClaim,
) -> None:
    """Record the claim the ledger confirmed as this intent's own receipt."""

    intent = load_intent(session, logical_key, revision_hash.value)
    receipt = EffectReceipt(
        intent,
        EffectId(confirmed.receipt.claim_id),
        EffectResult(confirmed.receipt.result_bytes()),
        confirmed.source,
    )
    commit_resolution(
        session, logical_key, revision_hash.value, encode_readback(receipt)
    )


def commit_work_item_claim_refusal(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    refusal: AgentExecutionRefusal,
) -> str:
    """End this node on its refusal, before an attempt of it ever exists."""

    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.AGENT_FAILED,
        refusal.value.encode("ascii"),
        RunState.STARTED,
        RunState.FAILED,
        node_id,
        terminal=True,
        round_ordinal=round_ordinal,
        target_round_ordinal=round_ordinal,
    ).state.value


def _confirmed_claim(receipt: ClaimReceipt) -> ClaimWorkItemReceipt:
    return ClaimWorkItemReceipt(
        receipt.item,
        receipt.claim_id,
        receipt.agent,
        receipt.branch,
        tuple(path.as_posix() for path in receipt.claimed_scope),
        tuple(
            ClaimedLanePath(
                touch.claim_id,
                touch.agent,
                tuple(path.as_posix() for path in touch.scope),
                touch.item,
            )
            for touch in receipt.touches
        ),
    )
