"""Every authored workflow document in `workflows/` is a real definition source.

A file under `workflows/` is authored the way an operator authors one for the
Catalog's Git-source intake (#660): the same bytes a `git pull` would hand the
publish door. This suite is the fixture-side half of that promise -- it proves
each committed document parses through the production V3 parser and clears
the same executability door `POST /workflow-revisions` enforces, so a document
that only looks executable never lands here unnoticed.

It also proves the documents a workflow *pins* are honest (#659): a `schema`
reference under `outputs` is not merely well-formed, it names a revision hash
that a shipped schema owner actually hashes to. Workflow-local schemas live in
`workflows/schemas/`; the house work-item and verdict schemas stay with their
production contracts. A workflow whose pin nobody could ever publish is exactly
as unstartable as one that fails to parse, so this suite treats both as the same
class of defect.

The catalog review workflows additionally pin the instruction that answers
against the required context order.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.schemas_v3 import SchemaAccepted, read_schema_document
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA
from atelier2.contracts.work_items import WORK_ITEM_ORDER_SCHEMA_DOCUMENT
from atelier2.contracts.workflow_executability import (
    what_a_v3_document_still_waits_for,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    VersionedReference,
    WorkflowGraphV3,
)

WORKFLOWS_DIRECTORY = Path(__file__).parents[2] / "workflows"
SCHEMAS_DIRECTORY = WORKFLOWS_DIRECTORY / "schemas"
AUTHORED_WORKFLOW_PATHS = sorted(WORKFLOWS_DIRECTORY.glob("*.yaml"))


@pytest.mark.parametrize(
    "workflow_path", AUTHORED_WORKFLOW_PATHS, ids=lambda path: path.name
)
def test_an_authored_workflow_document_is_executable(workflow_path: Path) -> None:
    graph = parse_executable_workflow_document(workflow_path.read_bytes())

    assert isinstance(graph, WorkflowGraphV3)
    assert what_a_v3_document_still_waits_for(graph) is None


def test_the_workflows_directory_carries_at_least_one_authored_document() -> None:
    assert AUTHORED_WORKFLOW_PATHS, "workflows/ must not be an empty fixture door"


def _schema_references(node: object) -> Iterator[VersionedReference]:
    """Every `schema` pin a parsed V3 document carries, walked once.

    `schema` is the one reference kind an authored workflow ships its own
    fixture document for (`workflows/schemas/`), so the walk is generic over
    the whole parsed tree by field name rather than naming each node and
    block that can carry one -- a new node kind that adds an output proves
    its schema pin here for free.
    """
    if isinstance(node, BaseModel):
        for name, value in node:
            if name == "schema_reference":
                assert isinstance(value, VersionedReference)
                yield value
            else:
                yield from _schema_references(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _schema_references(item)


def _authored_schema_pins() -> list[tuple[Path, VersionedReference]]:
    seen: set[tuple[str, str]] = set()
    pins: list[tuple[Path, VersionedReference]] = []
    for workflow_path in AUTHORED_WORKFLOW_PATHS:
        graph = parse_executable_workflow_document(workflow_path.read_bytes())
        for reference in _schema_references(graph):
            key = (reference.ref, reference.revision)
            if key in seen:
                continue
            seen.add(key)
            pins.append((workflow_path, reference))
    return pins


@pytest.mark.parametrize(
    ("workflow_path", "reference"),
    _authored_schema_pins(),
    ids=[
        f"{workflow_path.name}:{reference.ref}"
        for workflow_path, reference in _authored_schema_pins()
    ],
)
def test_a_pinned_schema_reference_resolves_to_a_shipped_document(
    workflow_path: Path, reference: VersionedReference
) -> None:
    if reference.ref == "work-item":
        document = WORK_ITEM_ORDER_SCHEMA_DOCUMENT
    elif reference.ref == "verdict":
        document = VERDICT_ANSWER_SCHEMA.document
    else:
        document_path = SCHEMAS_DIRECTORY / f"{reference.ref}.json"
        assert document_path.is_file(), (
            f"{workflow_path.name} pins schema {reference.ref!r}, but "
            f"{document_path} is not shipped in the repo"
        )
        document = document_path.read_bytes()
    assert PublishedRevisionHash.of(document).value == reference.revision, (
        f"{workflow_path.name} pins {reference.ref!r} at revision "
        f"{reference.revision}, but its shipped owner hashes to a different one"
    )
    verdict = read_schema_document(document)
    assert isinstance(verdict, SchemaAccepted), (
        f"{reference.ref!r} is not a schema the production profile accepts: {verdict}"
    )


CATALOG_REVIEW_WORKFLOW_PATHS = (
    WORKFLOWS_DIRECTORY / "code-review.yaml",
    WORKFLOWS_DIRECTORY / "plan-review.yaml",
)


@pytest.mark.parametrize(
    "workflow_path",
    CATALOG_REVIEW_WORKFLOW_PATHS,
    ids=("code-review", "plan-review"),
)
def test_a_catalog_review_instruction_judges_against_required_context(
    workflow_path: Path,
) -> None:
    graph = parse_executable_workflow_document(workflow_path.read_bytes())
    assert isinstance(graph, WorkflowGraphV3)
    review = graph.node("review")
    assert isinstance(review, AgentNodeV3)
    assert 'against the contract carried under "context"' in review.instruction
    assert "say which one wins and why" in review.instruction
    assert (
        'the head passes the explicit word "none" when it is truly absent'
        in review.instruction
    )
