"""What the library says a loose document is, from its bytes and its name alone."""

from __future__ import annotations

import pytest

from atelier2.adapters.markdown_agent_definitions import parse_agent_definition
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.classify_definition_document import (
    ClassifyDefinitionDocumentResult,
    classify_definition_document,
)
from atelier2.contracts.agents import ProviderId
from atelier2.contracts.library_recognition import (
    DocumentAmbiguous,
    DocumentNotHeld,
    DocumentUnrecognized,
    LibraryDocumentKind,
    RecognizedAgentDefinition,
    RecognizedWorkflow,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from tests.scenarios.workflows import V3_DOCUMENT

AGENT_DOCUMENT = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"description: Watches the stage and names what it sees.\n"
    b"---\n"
    b"You watch the stage and name what you see.\n"
)
V1_DOCUMENT = (
    b"format_version: 1\n"
    b"start: final\n"
    b"nodes:\n"
    b"  - {id: final, type: subworkflow, operation: add, operands: [1, 2], next: null}\n"
)
SKILL_DOCUMENT = (
    b"---\nname: stage-name-witness\nallowed-tools: Read\n---\nWatch the stage.\n"
)
"""A closed frontmatter block missing the agent minimum's `description`.

An unmodelled key like `allowed-tools` no longer refuses an agent reading on
its own (agent-definitions carry it through opaquely), so this fixture leans
on the still-required minimum to stay unambiguously a skill rather than an
agent definition.
"""
MCP_DECLARATION = b'{"mcpServers": {"github": {"command": "gh-mcp"}}}'


def recognise(
    document: bytes, file_name: str | None = None
) -> ClassifyDefinitionDocumentResult:
    return classify_definition_document(
        document, file_name, parse_workflow_document, parse_agent_definition
    )


@pytest.mark.parametrize(
    ("document", "file_name", "expected"),
    [
        pytest.param(
            V3_DOCUMENT,
            "workflows/review.yaml",
            RecognizedWorkflow(
                WorkflowFormatVersion.V3,
                "Implement a candidate, then review it for defects",
                None,
            ),
            id="v3-workflow-carries-its-authored-name",
        ),
        pytest.param(
            AGENT_DOCUMENT,
            "agents/witness.md",
            RecognizedAgentDefinition(
                "stage-name-witness",
                "Watches the stage and names what it sees.",
                ProviderId("anthropic"),
            ),
            id="agent-definition-carries-its-provider-mark",
        ),
        pytest.param(
            AGENT_DOCUMENT,
            None,
            RecognizedAgentDefinition(
                "stage-name-witness",
                "Watches the stage and names what it sees.",
                ProviderId("anthropic"),
            ),
            id="agent-definition-needs-no-file-name",
        ),
    ],
)
def test_a_held_kind_is_recognised_by_its_own_parser(
    document: bytes, file_name: str | None, expected: ClassifyDefinitionDocumentResult
) -> None:
    assert recognise(document, file_name) == expected


@pytest.mark.parametrize(
    ("document", "file_name", "kind"),
    [
        pytest.param(
            SKILL_DOCUMENT,
            "skills/witness/SKILL.md",
            LibraryDocumentKind.SKILL,
            id="skill-file-whose-frontmatter-is-no-agent",
        ),
        pytest.param(
            MCP_DECLARATION,
            ".mcp.json",
            LibraryDocumentKind.MCP_SERVER,
            id="mcp-declaration-by-its-servers-key",
        ),
        pytest.param(
            MCP_DECLARATION,
            None,
            LibraryDocumentKind.MCP_SERVER,
            id="mcp-declaration-needs-no-file-name",
        ),
    ],
)
def test_a_kind_the_library_does_not_hold_is_named_with_its_reason(
    document: bytes, file_name: str | None, kind: LibraryDocumentKind
) -> None:
    recognised = recognise(document, file_name)

    assert isinstance(recognised, DocumentNotHeld)
    assert recognised.kind is kind
    assert recognised.reason


def test_an_unrecognised_document_is_told_what_every_marker_expected() -> None:
    recognised = recognise(b"just a note to self\n", "notes.txt")

    assert isinstance(recognised, DocumentUnrecognized)
    by_kind = {refusal.kind: refusal for refusal in recognised.refusals}
    assert set(by_kind) == set(LibraryDocumentKind)
    assert "format_version" in by_kind[LibraryDocumentKind.WORKFLOW].expected
    assert "frontmatter-missing" in (
        by_kind[LibraryDocumentKind.AGENT_DEFINITION].refused_because
    )
    assert "SKILL.md" in by_kind[LibraryDocumentKind.SKILL].refused_because
    assert "JSON" in by_kind[LibraryDocumentKind.MCP_SERVER].refused_because


def test_a_workflow_missing_its_format_version_is_refused_by_that_field() -> None:
    recognised = recognise(b"name: no version\nnodes: []\n", "workflow.yaml")

    assert isinstance(recognised, DocumentUnrecognized)
    workflow = next(
        refusal
        for refusal in recognised.refusals
        if refusal.kind is LibraryDocumentKind.WORKFLOW
    )
    assert "format_version" in workflow.refused_because


def test_a_retired_workflow_format_is_refused_by_name() -> None:
    """No document may declare V1 anymore (#901 slice 5); it is refused, not held."""
    recognised = recognise(V1_DOCUMENT, "workflow.yaml")

    assert isinstance(recognised, DocumentUnrecognized)
    workflow = next(
        refusal
        for refusal in recognised.refusals
        if refusal.kind is LibraryDocumentKind.WORKFLOW
    )
    assert "unsupported" in workflow.refused_because


def test_a_skill_file_whose_frontmatter_is_a_valid_agent_is_refused_naming_both() -> (
    None
):
    """The name is a marker, not a tie-breaker: both claim, so neither is guessed."""

    assert recognise(AGENT_DOCUMENT, "SKILL.md") == DocumentAmbiguous(
        (LibraryDocumentKind.AGENT_DEFINITION, LibraryDocumentKind.SKILL)
    )


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(b"Just prose, no frontmatter.\n", id="no-frontmatter"),
        pytest.param(b"---\nname: witness\nnever closed\n", id="unterminated"),
        pytest.param(b"", id="empty"),
    ],
)
def test_a_skill_file_without_a_closed_frontmatter_block_is_not_a_skill(
    document: bytes,
) -> None:
    recognised = recognise(document, "SKILL.md")

    assert isinstance(recognised, DocumentUnrecognized)
    skill = next(
        refusal
        for refusal in recognised.refusals
        if refusal.kind is LibraryDocumentKind.SKILL
    )
    assert "closed frontmatter" in skill.refused_because
