"""The write-free recognition door: bytes and a name in, one typed answer out.

Every port a write would need is left unwired on purpose: a recognition that
touched the store would fail here, which is the sentence this door is for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.api.openapi import LIBRARY_RECOGNITIONS_PATH
from tests.scenarios.api import api_limits, described_api_client
from tests.scenarios.workflows import V3_DOCUMENT

AGENT_DOCUMENT = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"description: Watches the stage and names what it sees.\n"
    b"---\n"
    b"You watch the stage and name what you see.\n"
)
SKILL_DOCUMENT = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"allowed-tools: Read\n"
    b"---\n"
    b"You watch the stage and name what you see.\n"
)
"""A closed frontmatter block missing the agent minimum's `description`.

An unmodelled key like `allowed-tools` no longer refuses an agent reading on
its own (agent-definitions carry it through opaquely), so this fixture leans
on the still-required minimum to stay unambiguously a skill rather than an
agent definition.
"""
MCP_DECLARATION = b'{"mcpServers": {"github": {"command": "gh-mcp"}}}'


def recognise(
    api: TestClient,
    document: bytes,
    file_name: str | None = None,
    media_type: str = "application/octet-stream",
) -> Response:
    return api.post(
        LIBRARY_RECOGNITIONS_PATH,
        content=document,
        params={} if file_name is None else {"file_name": file_name},
        headers={"content-type": media_type},
    )


@pytest.mark.parametrize(
    ("document", "file_name", "expected"),
    [
        pytest.param(
            V3_DOCUMENT,
            "workflows/review.yaml",
            {
                "outcome": "workflow",
                "workflow_format_version": 3,
                "name": "Implement a candidate, then review it for defects",
                "description": None,
            },
            id="workflow",
        ),
        pytest.param(
            AGENT_DOCUMENT,
            None,
            {
                "outcome": "agent_definition",
                "name": "stage-name-witness",
                "description": "Watches the stage and names what it sees.",
                "provider_id": "anthropic",
            },
            id="agent-definition",
        ),
    ],
)
def test_a_held_kind_answers_its_authored_name_without_a_hash(
    document: bytes, file_name: str | None, expected: dict[str, object]
) -> None:
    answered = recognise(described_api_client(), document, file_name)

    assert answered.status_code == 200
    assert answered.json() == expected


@pytest.mark.parametrize(
    ("document", "file_name", "kind"),
    [
        pytest.param(SKILL_DOCUMENT, "SKILL.md", "skill", id="skill"),
        pytest.param(MCP_DECLARATION, ".mcp.json", "mcp_server", id="mcp-server"),
    ],
)
def test_a_kind_the_library_does_not_hold_says_so_with_a_reason(
    document: bytes, file_name: str, kind: str
) -> None:
    answered = recognise(described_api_client(), document, file_name)

    assert answered.status_code == 200
    body = answered.json()
    assert (body["outcome"], body["kind"]) == ("not_held", kind)
    assert body["reason"]


def test_an_unrecognised_document_names_what_each_kind_expected() -> None:
    answered = recognise(described_api_client(), b"a note\n", "notes.txt")

    assert answered.status_code == 200
    body = answered.json()
    assert body["outcome"] == "unrecognized"
    assert {refusal["kind"] for refusal in body["refusals"]} == {
        "workflow",
        "agent_definition",
        "skill",
        "mcp_server",
    }
    assert all(
        refusal["expected"] and refusal["refused_because"]
        for refusal in body["refusals"]
    )


def test_empty_bytes_are_unrecognized_with_one_refusal_per_kind() -> None:
    answered = recognise(described_api_client(), b"")

    assert answered.status_code == 200
    body = answered.json()
    assert body["outcome"] == "unrecognized"
    assert [refusal["kind"] for refusal in body["refusals"]] == [
        "workflow",
        "agent_definition",
        "skill",
        "mcp_server",
    ]


def test_a_skill_file_whose_frontmatter_is_a_valid_agent_is_refused_naming_both() -> (
    None
):
    refused = recognise(described_api_client(), AGENT_DOCUMENT, "SKILL.md")

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(":library-document-ambiguous")
    assert refused.json()["detail"] == (
        "The document matches agent_definition and skill."
    )


def test_the_door_refuses_any_media_type_but_opaque_bytes() -> None:
    refused = recognise(
        described_api_client(), V3_DOCUMENT, media_type="application/yaml"
    )

    assert refused.status_code == 415
    assert refused.json()["type"].endswith(":unsupported-media-type")


def test_a_file_name_beyond_the_field_bound_is_an_invalid_request() -> None:
    bound = api_limits().maximum_field_characters

    refused = recognise(described_api_client(), V3_DOCUMENT, "n" * (bound + 1))

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(":invalid-request")
