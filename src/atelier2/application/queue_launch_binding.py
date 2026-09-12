"""What a queue item's launch binding is proposed as, judged, and reserved.

Judging what a proposed binding's start would answer belongs to the sweep
that already knows how to start one (`advance_queue`), not here -- so
`resolved_launch_binding` asks for that judgment through `judge` rather than
importing the sweep back, which would make the two modules depend on each
other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from atelier2.application.queue_sweep_reads import (
    QueueAdvanceCorrupt,
    QueueAdvanceUnavailable,
    reserved_launch,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.queue_projection import (
    QueueBlockerKind,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogNameMissing,
    CatalogResolver,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.queue_projection import QueueProjection

_QUEUE_ITEM_RUN_DOMAIN = "queue-item-run/v2"


def proposed_launch_binding(
    item: QueueItemSnapshot, catalog: CatalogResolver
) -> QueueLaunchBinding | tuple[QueueBlockerKind, ...]:
    """The launch binding the item's admitted proposal names, unreserved.

    Raises `QueueAdvanceCorrupt` when the item does not carry one complete
    admitted proposal; returns the blockers in place of a binding wherever
    admission or the catalog leaves nothing to bind yet.
    """
    proposal = item.proposal
    admission = item.admission
    if (
        item.state is QueueItemState.ADMITTED
        and proposal is None
        and admission is not None
        and admission.authority is None
        and admission.proposal_revision is None
    ):
        return (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)
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
        return item.blockers
    revision_hash = _resolve_head(proposal.workflow_lineage_id, catalog)
    if revision_hash is None:
        return (QueueBlockerKind.BINDING_UNRESOLVED,)
    return QueueLaunchBinding(
        item.item_reference.item_id,
        admission.proposal_revision,
        _derive_run_id(item.item_reference.item_id, admission.proposal_revision.value),
        revision_hash,
    )


def resolved_launch_binding(
    item: QueueItemSnapshot,
    queue: QueueProjection,
    *,
    catalog: CatalogResolver,
    judge: Callable[[QueueItemSnapshot], tuple[QueueBlockerKind, ...] | None],
) -> QueueLaunchBinding | tuple[QueueBlockerKind, ...]:
    """The item's proposed binding, judged by `judge` and then reserved.

    `judge` sees the item carrying the proposed binding and answers the
    blockers a start of it would answer, or `None` when a start would not be
    refused -- asked before the reservation, so a start it would refuse
    never holds a place under the project's cap.
    """
    proposed = proposed_launch_binding(item, catalog)
    if isinstance(proposed, tuple):
        return proposed
    blockers = judge(replace(item, launch_binding=proposed))
    if blockers is not None:
        return blockers
    reserved = reserved_launch(queue, proposed)
    if isinstance(reserved, QueueLaunchBinding):
        return reserved
    return reserved.blockers


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
