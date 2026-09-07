"""A founded catalog lineage for scenarios that admit or launch queue items."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageFounded,
    CatalogLineageId,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import WorkflowRevision, WorkflowRevisionHash
from tests.scenarios.runs import publish_revision

BINDING_FREE_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")
"""The one schema a wait-only document needs published before it is executable.

`evaluate_executability` resolves every reference a V3 document pins, including
a Wait's declared output schema, before the start admits it -- so a line with no
agent role binding still needs this one pinned revision published, which
`found_lineage` does for every document it seats.
"""

BINDING_FREE_WORKFLOW = f"""format_version: 3
name: Binding-free wait line
nodes:
  - id: approve
    type: wait
    prompt: Add [2, 3].
    outputs:
      - name: approval
        schema: {{ref: approval-schema, revision: {BINDING_FREE_SCHEMA.revision_hash.value}}}
""".encode()
"""A wait-only document: startable without resolving any agent role binding.

No node here declares a role, so no agent executor is ever needed to admit it --
exactly what a queue-launch scenario wants to hold constant while it varies the
admission machinery around it. The bracketed pair inside the prompt plays the
role a differing operand pair once did: `.replace(...)` on it is what gives a
test a second, distinguishable revision of the same shape.
"""


def found_lineage(
    engine: Engine, document: bytes = BINDING_FREE_WORKFLOW
) -> tuple[CatalogLineageId, WorkflowRevisionHash]:
    """Publish `document` and seat it as a founded catalog lineage.

    The queue plans a proposal only against a lineage the catalog knows, so
    every scenario that admits or launches an item founds one first; the wait-
    only default keeps the lineage independent of any agent binding.
    """

    revision = WorkflowRevision(document)
    publish_revision(engine, revision)
    catalog = DbosCatalogStore(engine)
    catalog.publish_revision(BINDING_FREE_SCHEMA)
    published = PublishedRevision(RevisionKind.WORKFLOW, document)
    catalog.publish_revision(published)
    founded = catalog.found_lineage(
        published,
        CatalogLineageDisplayName(f"phase-d-{revision.revision_hash.value[:8]}"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-27T10:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded)
    return founded.lineage.lineage_id, revision.revision_hash
