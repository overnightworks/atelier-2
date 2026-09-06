from __future__ import annotations

import sys

import pytest

from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.application.reconstruct_agent_definition import (
    reconstruct_agent_definition,
)
from atelier2.contracts.agent_definitions import (
    MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS,
    MAXIMUM_AGENT_DEFINITION_TOOL_COUNT,
    AgentCatalogDeployment,
    AgentDefinition,
    AgentDefinitionField,
    AgentDefinitionRefusal,
    AgentDefinitionRefused,
    AgentToolName,
    DeclaredTools,
    UnrestrictedTools,
    agent_configuration_revision_for,
)
from atelier2.contracts.agents import AgentExecutorRevision, AuthProfileRevisionHash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind

AUTHORED_NAME = "stage-name-witness"
AUTHORED_DESCRIPTION = "Watches the stage and names what it sees."
AUTHORED_MODEL = "sonnet"
AUTHORED_PROMPT = "\nYou watch the stage and name what you see.\n"
TOO_MANY_TOOLS = MAXIMUM_AGENT_DEFINITION_TOOL_COUNT + 1


def test_published_revision_kinds_are_the_closed_catalog_set() -> None:
    assert tuple(kind.value for kind in RevisionKind) == (
        "workflow",
        "schema",
        "deterministic_operation",
        "adapter_operation",
        "context_source",
        "read_operation",
        "profile",
        "skill",
        "tool",
        "policy",
        "budget_policy",
        "retry_policy",
        "cancellation_policy",
        "scorecard_policy",
        "selection_policy",
        "admission_policy",
        "agent_definition",
    )


def authored_document(
    *,
    frontmatter: str = (
        f"name: {AUTHORED_NAME}\n"
        f"description: {AUTHORED_DESCRIPTION}\n"
        f"model: {AUTHORED_MODEL}\n"
    ),
    body: str = AUTHORED_PROMPT,
) -> bytes:
    return f"---\n{frontmatter}---\n{body}".encode()


def deployment(
    *,
    default_model: str = "claude-haiku-4-5",
    auth_profile_revision_hash: str = "a" * 64,
    executor_revision: str = "claude-cli/v1",
) -> AgentCatalogDeployment:
    return AgentCatalogDeployment(
        default_model,
        AuthProfileRevisionHash(auth_profile_revision_hash),
        AgentExecutorRevision(executor_revision),
    )


def refusal_of(document: bytes) -> tuple[AgentDefinitionRefusal, str | None]:
    with pytest.raises(AgentDefinitionRefused) as refused:
        parse_agent_definition(document)
    return refused.value.refusal, refused.value.subject


def test_a_markdown_file_authors_name_description_model_and_system_prompt() -> None:
    definition = parse_agent_definition(authored_document())

    assert definition.name == AUTHORED_NAME
    assert definition.description == AUTHORED_DESCRIPTION
    assert definition.model == AUTHORED_MODEL
    assert definition.system_prompt == AUTHORED_PROMPT


def test_the_body_is_the_system_prompt_byte_exact() -> None:
    body = "\n  Leading space, `---` inside a fence, ümlaut, trailing blanks.   \n\n"

    definition = parse_agent_definition(authored_document(body=body))

    assert definition.system_prompt.encode() == body.encode()


@pytest.mark.proves(
    "an-absent-tool-declaration-leaves-every-tool-and-a-declared-one-is-exact"
)
def test_an_absent_tools_field_leaves_the_agent_able_to_use_every_tool() -> None:
    definition = parse_agent_definition(authored_document())

    assert definition.tools == UnrestrictedTools()


@pytest.mark.proves(
    "an-absent-tool-declaration-leaves-every-tool-and-a-declared-one-is-exact"
)
def test_a_declared_tools_field_is_exactly_that_closed_set() -> None:
    definition = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools: Read, Grep\n"
        )
    )

    assert definition.tools == DeclaredTools(
        (AgentToolName("Grep"), AgentToolName("Read"))
    )


def test_both_spellings_of_a_tool_set_name_the_same_closed_set() -> None:
    comma_spelled = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools: Read, Grep\n"
        )
    )
    list_spelled = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools:\n  - Grep\n  - Read\n"
        )
    )

    assert comma_spelled == list_spelled


def test_an_empty_tool_list_restricts_the_agent_to_no_tool_at_all() -> None:
    definition = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools: []\n"
        )
    )

    assert definition.tools == DeclaredTools(())


def test_the_order_tools_are_typed_in_does_not_change_the_definition() -> None:
    def parsed(tools: str) -> AgentDefinition:
        return parse_agent_definition(
            authored_document(
                frontmatter=f"name: {AUTHORED_NAME}\n"
                f"description: {AUTHORED_DESCRIPTION}\n"
                f"tools: {tools}\n"
            )
        )

    assert parsed("Read, Grep, Bash") == parsed("Bash, Read, Grep")


@pytest.mark.proves("missing-or-unknown-frontmatter-is-refused-by-name")
@pytest.mark.parametrize(
    ("frontmatter", "refusal", "subject"),
    [
        pytest.param(
            f"description: {AUTHORED_DESCRIPTION}\n",
            AgentDefinitionRefusal.FIELD_MISSING,
            AgentDefinitionField.NAME.value,
            id="name-missing",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\n",
            AgentDefinitionRefusal.FIELD_MISSING,
            AgentDefinitionField.DESCRIPTION.value,
            id="description-missing",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            f"name: {AUTHORED_NAME}-again\n",
            AgentDefinitionRefusal.FIELD_DUPLICATED,
            AgentDefinitionField.NAME.value,
            id="duplicated-field",
        ),
        pytest.param(
            f"name: 12\ndescription: {AUTHORED_DESCRIPTION}\n",
            AgentDefinitionRefusal.FIELD_TYPE_UNEXPECTED,
            AgentDefinitionField.NAME.value,
            id="name-not-text",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\ntools: 12\n",
            AgentDefinitionRefusal.FIELD_TYPE_UNEXPECTED,
            AgentDefinitionField.TOOLS.value,
            id="tools-not-text-or-list",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools:\n  - [Read]\n",
            AgentDefinitionRefusal.FIELD_TYPE_UNEXPECTED,
            AgentDefinitionField.TOOLS.value,
            id="tool-entry-not-text",
        ),
        pytest.param(
            f'name: ""\ndescription: {AUTHORED_DESCRIPTION}\n',
            AgentDefinitionRefusal.FIELD_EMPTY,
            AgentDefinitionField.NAME.value,
            id="name-empty",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools: Read, , Grep\n",
            AgentDefinitionRefusal.FIELD_EMPTY,
            AgentDefinitionField.TOOLS.value,
            id="tool-name-empty",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools: Read, Grep, Read\n",
            AgentDefinitionRefusal.TOOL_DUPLICATED,
            "Read",
            id="tool-duplicated",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            "tools: "
            + ", ".join(f"tool-{index}" for index in range(TOO_MANY_TOOLS))
            + "\n",
            AgentDefinitionRefusal.TOO_MANY_TOOLS,
            str(TOO_MANY_TOOLS),
            id="too-many-tools",
        ),
    ],
)
def test_a_refused_frontmatter_names_what_it_refuses(
    frontmatter: str, refusal: AgentDefinitionRefusal, subject: str
) -> None:
    assert refusal_of(authored_document(frontmatter=frontmatter)) == (refusal, subject)


def _provider_native_document(*, body: str = AUTHORED_PROMPT) -> bytes:
    return (
        "---\n"
        f"description: {AUTHORED_DESCRIPTION}\n"
        f"name: {AUTHORED_NAME}\n"
        "permissionMode: acceptEdits\n"
        "skills:\n  - reviewing\n  - drafting\n"
        "---\n"
        f"{body}"
    ).encode()


def test_a_provider_native_key_the_atelier_does_not_model_is_accepted() -> None:
    definition = parse_agent_definition(_provider_native_document())

    assert definition.name == AUTHORED_NAME
    assert definition.description == AUTHORED_DESCRIPTION
    assert definition.model is None
    assert definition.tools == UnrestrictedTools()


def test_a_provider_native_keys_hash_is_stable() -> None:
    document = _provider_native_document()

    first = parse_agent_definition(document)
    second = parse_agent_definition(document)

    assert first.definition_hash == second.definition_hash


def test_a_provider_native_key_reconstructs_byte_identically() -> None:
    document = _provider_native_document(body="Prompt with a trailing blank line.\n\n")

    reconstructed = reconstruct_agent_definition(
        document, parse_agent_definition, render_agent_definition
    )

    assert reconstructed.revision.document == document
    assert render_agent_definition(reconstructed.definition) == document


@pytest.mark.parametrize(
    ("document", "refusal"),
    [
        pytest.param(
            b"You are an agent with no frontmatter.\n",
            AgentDefinitionRefusal.FRONTMATTER_MISSING,
            id="frontmatter-missing",
        ),
        pytest.param(
            f"---\nname: {AUTHORED_NAME}\n".encode(),
            AgentDefinitionRefusal.FRONTMATTER_UNTERMINATED,
            id="frontmatter-unterminated",
        ),
        pytest.param(
            b"---\n\xff\xfe\n---\nbody\n",
            AgentDefinitionRefusal.DOCUMENT_NOT_UTF8,
            id="document-not-utf8",
        ),
        pytest.param(
            authored_document(frontmatter="- a list, not a mapping\n"),
            AgentDefinitionRefusal.FRONTMATTER_NOT_A_MAPPING,
            id="frontmatter-not-a-mapping",
        ),
        pytest.param(
            authored_document(frontmatter="name: [unclosed\n"),
            AgentDefinitionRefusal.FRONTMATTER_UNPARSABLE,
            id="frontmatter-unparsable",
        ),
        pytest.param(
            authored_document(body="\n   \n"),
            AgentDefinitionRefusal.SYSTEM_PROMPT_MISSING,
            id="system-prompt-missing",
        ),
        pytest.param(
            authored_document(
                body="x" * (MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS + 1)
            ),
            AgentDefinitionRefusal.DOCUMENT_TOO_LARGE,
            id="document-too-large",
        ),
    ],
)
def test_a_refused_document_names_why(
    document: bytes, refusal: AgentDefinitionRefusal
) -> None:
    assert refusal_of(document)[0] is refusal


def test_frontmatter_nested_beyond_the_parsers_recursion_is_refused_by_name() -> None:
    beyond_the_interpreters_stack = sys.getrecursionlimit() * 2
    nested = "[" * beyond_the_interpreters_stack + "]" * beyond_the_interpreters_stack

    assert (
        refusal_of(authored_document(frontmatter=f"name: {nested}\n"))[0]
        is AgentDefinitionRefusal.FRONTMATTER_UNPARSABLE
    )


@pytest.mark.proves("the-same-file-is-the-same-revision-identity")
def test_the_same_file_is_the_same_definition() -> None:
    first = parse_agent_definition(authored_document())
    second = parse_agent_definition(authored_document())

    assert first == second
    assert first.definition_hash == second.definition_hash


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param(
            f"name: other-witness\ndescription: {AUTHORED_DESCRIPTION}\n"
            f"model: {AUTHORED_MODEL}\n",
            id="other-name",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: Watches something else.\n"
            f"model: {AUTHORED_MODEL}\n",
            id="other-description",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\n"
            "model: opus\n",
            id="other-model",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\n",
            id="model-absent",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\n"
            f"model: {AUTHORED_MODEL}\ntools: Read\n",
            id="tools-declared",
        ),
    ],
)
def test_every_authored_difference_is_a_different_definition(frontmatter: str) -> None:
    authored = parse_agent_definition(authored_document())
    varied = parse_agent_definition(authored_document(frontmatter=frontmatter))

    assert varied.definition_hash != authored.definition_hash


def test_a_different_system_prompt_is_a_different_definition() -> None:
    authored = parse_agent_definition(authored_document())
    varied = parse_agent_definition(authored_document(body="\nYou watch nothing.\n"))

    assert varied.definition_hash != authored.definition_hash


def test_an_unrestricted_agent_is_not_an_agent_declaring_no_tool() -> None:
    unrestricted = parse_agent_definition(authored_document())
    restricted = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            f"model: {AUTHORED_MODEL}\ntools: []\n"
        )
    )

    assert restricted.definition_hash != unrestricted.definition_hash


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\n"
            f"model: {AUTHORED_MODEL}\n",
            id="every-field",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\n",
            id="model-absent",
        ),
        pytest.param(
            f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\n"
            "tools: Bash, Grep, Read\n",
            id="tools-declared",
        ),
    ],
)
def test_a_definition_survives_rendering_and_parsing_unchanged(
    frontmatter: str,
) -> None:
    definition = parse_agent_definition(authored_document(frontmatter=frontmatter))

    assert parse_agent_definition(render_agent_definition(definition)) == definition


def test_a_tool_name_spelling_the_comma_separator_survives_rendering() -> None:
    definition = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            'tools:\n  - "Read,Grep"\n'
        )
    )

    assert definition.tools == DeclaredTools((AgentToolName("Read,Grep"),))
    assert parse_agent_definition(render_agent_definition(definition)) == definition


def test_a_rendered_document_is_reparsed_into_the_same_bytes() -> None:
    document = render_agent_definition(parse_agent_definition(authored_document()))

    assert render_agent_definition(parse_agent_definition(document)) == document


def test_a_description_carrying_yaml_punctuation_survives_rendering() -> None:
    written = '"Reviews: plans, diffs & prose - honestly.\\nOn two lines."'
    definition = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\ndescription: {written}\n"
        )
    )

    assert "\n" in definition.description
    assert (
        parse_agent_definition(render_agent_definition(definition)).description
        == definition.description
    )


@pytest.mark.proves("a-markdown-file-becomes-a-published-configuration-revision")
def test_the_same_file_publishes_the_same_configuration_revision() -> None:
    first = agent_configuration_revision_for(
        parse_agent_definition(authored_document()), deployment()
    )
    second = agent_configuration_revision_for(
        parse_agent_definition(authored_document()), deployment()
    )

    assert first.revision_hash == second.revision_hash


def test_an_authored_model_is_the_configured_model() -> None:
    revision = agent_configuration_revision_for(
        parse_agent_definition(authored_document()), deployment()
    )

    assert revision.model == AUTHORED_MODEL


def test_an_agent_without_a_model_takes_the_deployments_default() -> None:
    definition = parse_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\ndescription: {AUTHORED_DESCRIPTION}\n"
        )
    )

    revision = agent_configuration_revision_for(
        definition, deployment(default_model="claude-opus-5")
    )

    assert revision.model == "claude-opus-5"


def test_the_deployment_owns_the_auth_and_executor_the_file_never_spells() -> None:
    binding = deployment(
        auth_profile_revision_hash="b" * 64, executor_revision="cli/v2"
    )

    revision = agent_configuration_revision_for(
        parse_agent_definition(authored_document()), binding
    )

    assert revision.auth_profile_revision_hash == binding.auth_profile_revision_hash
    assert revision.executor_revision == binding.executor_revision


def test_an_reconstructed_agent_definition_preserves_name_description_prompt_and_tools() -> (
    None
):
    reconstructed = reconstruct_agent_definition(
        authored_document(
            frontmatter=f"name: {AUTHORED_NAME}\n"
            f"description: {AUTHORED_DESCRIPTION}\n"
            f"model: {AUTHORED_MODEL}\n"
            "tools: Read, Grep\n",
            body="\nReview the prompt as authored.\n",
        ),
        parse_agent_definition,
        render_agent_definition,
    )
    reparsed = parse_agent_definition(reconstructed.revision.document)

    assert reparsed.name == AUTHORED_NAME
    assert reparsed.description == AUTHORED_DESCRIPTION
    assert reparsed.system_prompt == "\nReview the prompt as authored.\n"
    assert reparsed.tools == DeclaredTools(
        (AgentToolName("Grep"), AgentToolName("Read"))
    )
    assert reparsed == reconstructed.definition
    assert reconstructed.revision.kind is RevisionKind.AGENT_DEFINITION


def test_reconstructing_noncanonical_frontmatter_keeps_exact_input_bytes() -> None:
    original = authored_document(
        frontmatter=f"name: {AUTHORED_NAME}\n"
        f"description: {AUTHORED_DESCRIPTION}\n"
        f"model: {AUTHORED_MODEL}\n"
        "tools: Read, Bash\n",
        body="\nReview the prompt as authored.\n",
    )
    reconstructed = reconstruct_agent_definition(
        original,
        parse_agent_definition,
        render_agent_definition,
    )

    assert reconstructed.revision.document == original
    assert reconstructed.revision.revision_hash == PublishedRevisionHash.of(original)


def test_agent_definition_revisions_differ_when_only_the_prompt_differs() -> None:
    watching = reconstruct_agent_definition(
        authored_document(),
        parse_agent_definition,
        render_agent_definition,
    )
    idling = reconstruct_agent_definition(
        authored_document(body="\nYou watch nothing.\n"),
        parse_agent_definition,
        render_agent_definition,
    )

    assert watching.revision.document != idling.revision.document
    assert watching.revision.revision_hash != idling.revision.revision_hash
