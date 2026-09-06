"""The run's sole persisted work-item order, and what an open-pr intent derives from it.

Both open-pr intent builders in `advancer.py` -- the Action-shaped and the
Agent-grant-shaped one -- need the same run-scoped work item: its head branch
when the run pushes to a bound project, and its tracker reference for the pull
request's `Work-Item`/`Closes` lines. Reading the run's one issue work-item
order and deriving both from it belongs to one owner instead of each builder
reading the durable input rows itself.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.schema import run_inputs_v3
from atelier2.contracts.effect_requests import HeadBranch, head_branch_for_queue_item
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference, WorkItemReference
from atelier2.contracts.runs import RunId
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    WorkItemKind,
    WorkItemOrderDocument,
    read_work_item_order_document,
)


class WorkItemOrderConflict(RuntimeError):
    pass


def issue_work_item_order(session: Any, run_id: RunId) -> WorkItemOrderDocument:
    """The one issue work-item order this run was bound to."""
    rows = session.execute(
        sa.select(
            run_inputs_v3.c.schema_revision_hash,
            run_inputs_v3.c.value,
            run_inputs_v3.c.value_hash,
        ).where(run_inputs_v3.c.run_id == run_id.value)
    ).all()
    orders = []
    for schema_revision, value, value_hash in rows:
        raw = bytes(value)
        if Sha256Hash.of(raw).value != str(value_hash):
            raise WorkItemOrderConflict(
                "run input bytes differ from their durable hash"
            )
        if str(schema_revision) != WORK_ITEM_ORDER_SCHEMA_REVISION.value:
            continue
        order = read_work_item_order_document(raw)
        if order is None or order.kind is not WorkItemKind.ISSUE:
            raise WorkItemOrderConflict(
                "the run requires one valid issue work-item order"
            )
        orders.append(order)
    if len(orders) != 1:
        raise WorkItemOrderConflict(
            "the run requires exactly one issue work-item order"
        )
    return orders[0]


def head_branch_for_work_item(
    work_item: WorkItemOrderDocument, project_id: ProjectId
) -> HeadBranch:
    return head_branch_for_queue_item(
        WorkItemReference(project_id, work_item.reference).item_id
    )


def open_pr_work_item_reference(
    work_item: WorkItemOrderDocument | None,
) -> TrackerItemReference | None:
    """The tracker reference an `open_pr` intent carries for this run's work item."""
    return None if work_item is None else work_item.reference
