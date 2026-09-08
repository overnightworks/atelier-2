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
from pathlib import Path, PurePosixPath
from typing import Any, cast

from dbos import SQLAlchemyDatasource

from atelier2.adapters.dbos.advancer import (
    effect_receipt_exists,
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
from atelier2.adapters.dbos.names import (
    WORK_ITEM_CLAIM_CONFIRM_STEP_NAME,
    WORK_ITEM_CLAIM_HOLD_STEP_NAME,
    WORK_ITEM_CLAIM_PREPARE_STEP_NAME,
    WORK_ITEM_CLAIM_REFUSE_STEP_NAME,
)
from atelier2.adapters.dbos.queue_launch_runs import launch_binding_of_run
from atelier2.adapters.dbos.run_transitions import _commit_event, load_graph
from atelier2.adapters.dbos.work_item_intents import (
    head_branch_for_work_item,
    issue_work_item_order,
)
from atelier2.adapters.github.tracker_reference import github_issue_number_or_none
from atelier2.application.bind_node import pinned_project
from atelier2.application.queue_sweep_reads import active_policy
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    ClaimedLanePath,
    ClaimReasons,
    ClaimWorkItem,
    ClaimWorkItemReceipt,
    HeadBranch,
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
    AgentNodeRefusalRecord,
    RunEventKind,
    logical_effect_key_for_work_item_claim,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.node_bindings import AgentNodeBindingV2
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.ports.claim_checkouts import (
    ClaimCheckoutRefused,
    ClaimCheckouts,
    ClaimCheckoutUnavailable,
)
from atelier2.ports.project_source import ProjectSourceUnavailable
from atelier2.ports.project_verification import DeclaredProject
from atelier2.ports.queue_projection import QueuePolicyReader
from atelier2.ports.work_item_claims import (
    ClaimAbsent,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimTouch,
    WorkItemClaims,
    _bounded_detail,
)

LOGICAL_KEY_FIELD = "logical_key"
BRANCH_FIELD = "branch"
REFUSAL_FIELD = "refusal"
HELD_FIELD = "held"
WHOLE_SCOPE_REASON = (
    "der Lauf claimt genau den Scope, den der Item-Body unter `## Dateien` "
    "regelt; der Schnitt ist der des Items"
)
"""Why the ledger's width check is waived: the scope is the item's own cut."""


def out_of_order_reason(label: str) -> str:
    """Why the ledger's board-order check is waived for a queue-launched run."""

    return (
        f"vom Operator per Label `{label}` zugelassen; die Board-Reihenfolge war "
        "bei der Zulassung entschieden"
    )


_UNCONFIGURED_CLAIM_LEDGER = AgentExecutionRefusal.WORK_ITEM_CLAIM_UNCONFIGURED.value

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
    """The claim boundary this runtime holds, the binding it records with, the
    queue policy that says which label admits a run out of board order, and
    the checkouts the claim is held from."""

    claims: WorkItemClaims
    binding: EffectAdapterBinding
    policy: QueuePolicyReader
    checkouts: ClaimCheckouts


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

    `detail` is the sentence that names why: the ledger's own word, or this
    runtime's sentence from the granted claim's touches, and empty where
    neither said more. A claim the ledger granted over paths another lane
    already holds is a fact that happened: it is recorded as this effect's
    receipt, and the node ends afterwards. Releasing it belongs to the run's
    own completion.
    """

    reason: AgentExecutionRefusal
    detail: str = ""
    confirmed: ConfirmedWorkItemClaim | None = None

    def record(self) -> AgentNodeRefusalRecord:
        return AgentNodeRefusalRecord(self.reason, self.detail)


type WorkItemClaimOutcome = WorkItemClaimHeld | WorkItemClaimRefused


def prepare_work_item_claim(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    ledger: WorkItemClaimLedger | None,
    project_id: ProjectId | None,
) -> dict[str, str] | None:
    """Record the claim this node owes before any command runs, or name why not.

    Answers nothing where the node changes nothing: only a node whose own
    pinned grant publishes a commit holds its item's claim. Otherwise it
    answers the claim already receipted for this execution, the prepared
    intent's logical key, or the refusal word this node ends on -- a runtime
    that cannot claim never quietly builds unclaimed.
    """

    node = load_graph(session, revision_hash).node(node_id)
    grant = read_pinned_effect_tool_grant(session, node)
    if push_atelier_commit_capability_for(grant) is None:
        return None
    logical_key = logical_effect_key_for_work_item_claim(
        run_id, revision_hash, node_id, round_ordinal
    )
    if effect_receipt_exists(session, logical_key.value):
        return {HELD_FIELD: logical_key.value}
    if ledger is None or project_id is None:
        return {REFUSAL_FIELD: _UNCONFIGURED_CLAIM_LEDGER}
    request = _requested_claim(session, run_id, project_id, ledger.policy)
    if isinstance(request, AgentExecutionRefusal):
        return {REFUSAL_FIELD: request.value}
    # The claim's own binding is held beside the graph's effect adapters rather
    # than among them: no published document declares this operation, so no
    # graph node can select it.
    binding = EffectBinding(
        logical_key,
        run_id,
        revision_hash,
        ledger.binding.adapter_revision,
        ledger.binding.destination,
        ledger.binding.operational_identity,
        AdapterOperationName.CLAIM_WORK_ITEM,
    )
    intent = EffectIntent(binding, CanonicalRequest(request.canonical_bytes()))
    prepared = prepared_effect_intent(session, intent)
    return {
        LOGICAL_KEY_FIELD: prepared.intent.binding.logical_key.value,
        BRANCH_FIELD: request.head_branch.value,
    }


def _requested_claim(
    session: Any, run_id: RunId, project_id: ProjectId, queue: QueuePolicyReader
) -> ClaimWorkItem | AgentExecutionRefusal:
    """The claim this run's own work-item order asks for, or why it asks none."""

    order = issue_work_item_order(session, run_id)
    item = github_issue_number_or_none(order.reference)
    if item is None:
        return AgentExecutionRefusal.WORK_ITEM_CLAIM_UNCONFIGURED
    if not order.scope.paths:
        return AgentExecutionRefusal.WORK_ITEM_NAMES_NO_SCOPE
    return ClaimWorkItem(
        item,
        work_item_claim_id(run_id, item),
        head_branch_for_work_item(order, project_id),
        order.scope.paths,
        ClaimReasons(
            WHOLE_SCOPE_REASON, _admission_reason(session, run_id, project_id, queue)
        ),
    )


def _admission_reason(
    session: Any, run_id: RunId, project_id: ProjectId, queue: QueuePolicyReader
) -> str | None:
    """The out-of-order reason a queue admission gives this run, or none.

    Only a run the queue started under a policy that names its label was
    admitted by the operator; a hand-started run carries no such decision and
    is refused by priority as a person would be.
    """

    if launch_binding_of_run(session.connection(), run_id) is None:
        return None
    policy = active_policy(queue, project_id)
    if policy is None or policy.automation_label is None:
        return None
    return out_of_order_reason(policy.automation_label)


def hold_prepared_claim(
    intent: EffectIntent, ledger: WorkItemClaimLedger, checkout: Path
) -> WorkItemClaimOutcome:
    """Take the prepared claim, reading the ledger back before asking for it.

    The claim id is this run's own, so a claim an earlier attempt already
    posted is recognised here instead of being taken twice -- and a ledger that
    answers something other than this exact request is refused rather than
    built on. Both are asked from `checkout`, the run's own claim checkout.
    """

    request = ClaimWorkItem.from_canonical_bytes(intent.request.payload)
    scope = tuple(PurePosixPath(path) for path in request.scope)
    standing = ledger.claims.read_back(request.item, request.claim_id, checkout)
    source = ConfirmationSource.ADAPTER_READBACK
    if isinstance(standing, ClaimRefusal):
        return WorkItemClaimRefused(_REFUSAL_WORDS[standing.reason], standing.detail)
    if isinstance(standing, ClaimAbsent):
        source = ConfirmationSource.ADAPTER_EXECUTION
        standing = ledger.claims.claim(
            request.item,
            intent.binding.run_id,
            request.head_branch,
            scope,
            request.claim_id,
            request.reasons,
            checkout,
        )
        if isinstance(standing, ClaimRefusal):
            return WorkItemClaimRefused(
                _REFUSAL_WORDS[standing.reason], standing.detail
            )
    held: ClaimReceipt = standing
    confirmed = ConfirmedWorkItemClaim(_confirmed_claim(held), source)
    if (
        held.item != request.item
        or held.claim_id != request.claim_id
        or held.branch != request.head_branch
        or held.claimed_scope != scope
    ):
        # The ledger granted this run's own claim id over another branch or
        # another scope: the grant exists and this run may not build under it,
        # so it is receipted as it stands -- an unreceipted grant would be a
        # claim nothing can ever release.
        return WorkItemClaimRefused(
            AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED, confirmed=confirmed
        )
    if held.touches:
        return WorkItemClaimRefused(
            AgentExecutionRefusal.WORK_ITEM_CLAIM_TOUCHES_ANOTHER_LANE,
            _bounded_detail(_touching_lane_detail(held)),
            confirmed=confirmed,
        )
    return WorkItemClaimHeld(confirmed)


def _touching_lane_detail(held: ClaimReceipt) -> str:
    """Name each touching lane by the paths that actually collide with this claim.

    The foreign scope is an inventory; the operator needs the intersection.
    """

    return "; ".join(
        _touch_clause(held.claimed_scope, touch)
        for touch in _ordered_touches(held.touches)
    )


def _ordered_touches(touches: tuple[ClaimTouch, ...]) -> tuple[ClaimTouch, ...]:
    return tuple(
        sorted(
            touches,
            key=lambda touch: (
                touch.item is None,
                touch.item if touch.item is not None else 0,
                touch.claim_id,
            ),
        )
    )


def _touch_clause(claimed_scope: tuple[PurePosixPath, ...], touch: ClaimTouch) -> str:
    lane = f"item {touch.item}" if touch.item is not None else touch.agent
    paths = _colliding_paths(claimed_scope, touch.scope)
    if not paths:
        return lane
    return f"{lane} on {paths}"


def _colliding_paths(
    claimed_scope: tuple[PurePosixPath, ...],
    foreign_scope: tuple[PurePosixPath, ...],
) -> str:
    colliding: list[str] = []
    for ours in claimed_scope:
        for theirs in foreign_scope:
            if ours == theirs or ours.is_relative_to(theirs):
                colliding.append(ours.as_posix())
            elif theirs.is_relative_to(ours):
                colliding.append(theirs.as_posix())
    if not colliding:
        colliding = [path.as_posix() for path in foreign_scope]
    return ", ".join(sorted(dict.fromkeys(colliding)))


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
    refusal: AgentNodeRefusalRecord,
) -> str:
    """End this node on its refusal, before an attempt of it ever exists."""

    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.AGENT_FAILED,
        refusal.encode(),
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


def refuse_unattested_pin(
    datasource: SQLAlchemyDatasource,
    binding: AgentNodeBindingV2,
    project: DeclaredProject | None,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> str | None:
    """End the node when the pin it was bound to can no longer be answered for.

    Opening a claim for work this host cannot begin would hold a lane forever.
    The source's own sentence travels as the refusal detail, through the same
    scrub and bound every ledger refusal uses; the closed word is the claim
    door's generic refusal, not a new vocabulary.
    """

    pinned = pinned_project(binding, project)
    if pinned is None:
        return None
    try:
        pinned.source.attest(pinned.pin)
    except ProjectSourceUnavailable as error:
        return _refuse_claim(
            datasource,
            run_id,
            revision_hash,
            node_id,
            binding.round_ordinal,
            AgentNodeRefusalRecord(
                AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED,
                _bounded_detail(str(error)),
            ),
        )
    return None


def hold_work_item_claim(
    datasource: SQLAlchemyDatasource,
    ledger: WorkItemClaimLedger | None,
    project_id: ProjectId | None,
    binding: AgentNodeBindingV2,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> str | None:
    """Hold this node's work-item claim, or end the node where it cannot.

    Runs before the attempt is executed, so a node that may not claim never
    leases a workspace, never starts a provider and never pushes. The intent is
    written durably before the ledger is asked, the ledger's answer is taken in
    one durable step, and the claim it confirms is recorded before the builder
    is allowed to begin. A recovery replays the answer that step recorded and
    never asks the ledger again, so it cannot refuse work already under way on
    a newer answer, nor build on a grant the node already ended on.

    The claim is asked from the run's claim checkout on the lane branch at the
    node's pin. Opening it is no durable step: it is idempotent by run, so a
    replay finds the checkout again or makes it anew, and the memoized hold
    still answers without the ledger. A refusal closes it; a held claim keeps
    it until the claim is released.

    Answers `None` where the node may work, and the run's own terminal state
    where it may not.
    """

    round_ordinal = binding.round_ordinal
    prepared = _prepared_claim(
        datasource, ledger, project_id, run_id, revision_hash, node_id, round_ordinal
    )
    if prepared is None or HELD_FIELD in prepared:
        return None
    refused = prepared.get(REFUSAL_FIELD)
    if refused is not None:
        return _refuse_claim(
            datasource,
            run_id,
            revision_hash,
            node_id,
            round_ordinal,
            AgentNodeRefusalRecord(AgentExecutionRefusal(refused)),
        )
    if ledger is None:
        raise RunBindingConflict(
            "a prepared work-item claim requires the ledger that bound it"
        )
    return _drive_prepared_claim(
        datasource,
        ledger,
        binding,
        run_id,
        revision_hash,
        node_id,
        round_ordinal,
        prepared,
    )


def _drive_prepared_claim(
    datasource: SQLAlchemyDatasource,
    ledger: WorkItemClaimLedger,
    binding: AgentNodeBindingV2,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    prepared: dict[str, str],
) -> str | None:
    """Open this run's checkout, take the memoized hold, and refuse when it cannot.

    Opening is no durable step. A typed checkout failure is recorded as this
    hold's own refusal so a replay that cannot open still consumes the hold
    step, then takes the refuse step, instead of raising out of the node.
    Closing the checkout on that path is best-effort: a close that cannot
    finish must not replace the refusal, so the refuse step still runs.
    """

    logical_key = prepared[LOGICAL_KEY_FIELD]
    checkout: Path | None
    unavailable: BaseException | None
    try:
        checkout = _opened_claim_checkout(ledger, binding, run_id, prepared)
    except (ClaimCheckoutUnavailable, ClaimCheckoutRefused) as error:
        checkout = None
        unavailable = error
    else:
        unavailable = None
    outcome = _held_claim(
        datasource, ledger, logical_key, revision_hash, checkout, unavailable
    )
    _confirm_claim(datasource, logical_key, revision_hash, outcome)
    if isinstance(outcome, WorkItemClaimHeld):
        return None
    try:
        ledger.checkouts.close(run_id)
    except ClaimCheckoutUnavailable:
        pass
    return _refuse_claim(
        datasource, run_id, revision_hash, node_id, round_ordinal, outcome.record()
    )


def _opened_claim_checkout(
    ledger: WorkItemClaimLedger,
    binding: AgentNodeBindingV2,
    run_id: RunId,
    prepared: dict[str, str],
) -> Path:
    """The run's claim checkout on the prepared lane branch at the node's pin."""

    if binding.project_source is None:
        raise RunBindingConflict(
            "a prepared work-item claim requires the project pin its node was bound to"
        )
    return ledger.checkouts.open(
        run_id, HeadBranch(prepared[BRANCH_FIELD]), binding.project_source
    )


def _prepared_claim(
    datasource: SQLAlchemyDatasource,
    ledger: WorkItemClaimLedger | None,
    project_id: ProjectId | None,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
) -> dict[str, str] | None:
    """Write this node's claim intent before any command runs, or answer why not."""

    return cast(
        dict[str, str] | None,
        datasource.run_tx_step(
            {"name": WORK_ITEM_CLAIM_PREPARE_STEP_NAME},
            lambda: prepare_work_item_claim(
                datasource.sql_session(),
                run_id,
                revision_hash,
                node_id,
                round_ordinal,
                ledger,
                project_id,
            ),
        ),
    )


def _held_claim(
    datasource: SQLAlchemyDatasource,
    ledger: WorkItemClaimLedger,
    logical_key: str,
    revision_hash: WorkflowRevisionHash,
    checkout: Path | None,
    unavailable: BaseException | None,
) -> WorkItemClaimOutcome:
    """Ask the ledger once, inside the one durable step that records its answer.

    The outcome is what the step memoizes, so a recovery replays the decision
    this drive took instead of asking the ledger again: a claim that was held
    stays held while the attempt runs, and a refusal stays the one refusal the
    node ended on. Only a drive that dies before the step is recorded asks
    again, and then it reads its own claim back rather than taking a second.

    A checkout that could not be opened is that refusal: the step records it
    without asking the ledger, so a replay whose `open` fails still consumes
    this step and can take the refuse step that ends the node.
    """

    def take() -> WorkItemClaimOutcome:
        if checkout is None:
            return WorkItemClaimRefused(
                AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED,
                _bounded_detail(str(unavailable)),
            )
        return hold_prepared_claim(
            load_intent(datasource.sql_session(), logical_key, revision_hash.value),
            ledger,
            checkout,
        )

    return cast(
        WorkItemClaimOutcome,
        datasource.run_tx_step({"name": WORK_ITEM_CLAIM_HOLD_STEP_NAME}, take),
    )


def _confirm_claim(
    datasource: SQLAlchemyDatasource,
    logical_key: str,
    revision_hash: WorkflowRevisionHash,
    outcome: WorkItemClaimOutcome,
) -> None:
    """Record the claim the ledger granted, whatever the node does next.

    A claim that touches another lane, or that the ledger recorded over
    another branch or scope, still exists: its receipt is written before the
    node ends on it, so nothing is left granted and unaccounted for.
    """

    confirmed = outcome.confirmed
    if confirmed is None:
        return
    datasource.run_tx_step(
        {"name": WORK_ITEM_CLAIM_CONFIRM_STEP_NAME},
        lambda: confirm_work_item_claim(
            datasource.sql_session(), logical_key, revision_hash, confirmed
        ),
    )


def _refuse_claim(
    datasource: SQLAlchemyDatasource,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    refusal: AgentNodeRefusalRecord,
) -> str:
    return str(
        datasource.run_tx_step(
            {"name": WORK_ITEM_CLAIM_REFUSE_STEP_NAME},
            lambda: commit_work_item_claim_refusal(
                datasource.sql_session(),
                run_id,
                revision_hash,
                node_id,
                round_ordinal,
                refusal,
            ),
        )
    )
