from __future__ import annotations

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from atelier2.contracts.agent_definitions import (
    MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS,
    REQUIRED_AGENT_DEFINITION_FIELDS,
    AgentDefinition,
    AgentDefinitionField,
    AgentDefinitionRefusal,
    AgentDefinitionRefused,
    AgentToolDeclaration,
    AgentToolName,
    DeclaredTools,
    UnrestrictedTools,
)

FRONTMATTER_DELIMITER = "---"
TOOL_SEPARATOR = ","
_YAML_TEXT_TAG = "tag:yaml.org,2002:str"


def parse_agent_definition(document: bytes) -> AgentDefinition:
    """Read one authored markdown agent definition, or refuse it by name."""

    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.DOCUMENT_NOT_UTF8
        ) from error
    if len(text) > MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS:
        raise AgentDefinitionRefused(AgentDefinitionRefusal.DOCUMENT_TOO_LARGE)
    frontmatter, system_prompt = _split_frontmatter(text)
    fields = _frontmatter_fields(frontmatter)
    for required in REQUIRED_AGENT_DEFINITION_FIELDS:
        if required.value not in fields:
            raise AgentDefinitionRefused(
                AgentDefinitionRefusal.FIELD_MISSING, required.value
            )
    return AgentDefinition(
        _authored_text(
            fields[AgentDefinitionField.NAME.value], AgentDefinitionField.NAME
        ),
        _authored_text(
            fields[AgentDefinitionField.DESCRIPTION.value],
            AgentDefinitionField.DESCRIPTION,
        ),
        _authored_model(fields),
        _tool_declaration(fields),
        system_prompt,
        document,
    )


def render_agent_definition(definition: AgentDefinition) -> bytes:
    """Return the exact bytes the definition was authored from.

    Every key beyond the parsed minimum, the key order, and the file's exact
    whitespace are opaque to this parser but not lost to it: they live in the
    document the definition already carries, so reconstruction is that
    document handed back rather than a second, rebuilt spelling.
    """

    return definition.document


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0] != f"{FRONTMATTER_DELIMITER}\n":
        raise AgentDefinitionRefused(AgentDefinitionRefusal.FRONTMATTER_MISSING)
    for index, line in enumerate(lines[1:], start=1):
        if line in (f"{FRONTMATTER_DELIMITER}\n", FRONTMATTER_DELIMITER):
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise AgentDefinitionRefused(AgentDefinitionRefusal.FRONTMATTER_UNTERMINATED)


def _frontmatter_fields(frontmatter: str) -> dict[str, Node]:
    """Read the parsed minimum's nodes out of the frontmatter mapping.

    A key outside the parsed minimum is neither read nor refused here: it
    stays unindexed, opaque, and present only in the document's own bytes,
    which is where a provider-native key -- and the file's exact spelling --
    is carried through to reconstruction.
    """

    try:
        root = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError) as error:
        # A document nested deeper than the composer can recurse is a document
        # this parser cannot read, and an external file must never reach a
        # caller as a bare interpreter error instead of a named refusal.
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.FRONTMATTER_UNPARSABLE
        ) from error
    if not isinstance(root, MappingNode):
        raise AgentDefinitionRefused(AgentDefinitionRefusal.FRONTMATTER_NOT_A_MAPPING)
    known = {field.value for field in AgentDefinitionField}
    fields: dict[str, Node] = {}
    for key_node, value_node in root.value:
        if not isinstance(key_node, ScalarNode):
            raise AgentDefinitionRefused(
                AgentDefinitionRefusal.FRONTMATTER_NOT_A_MAPPING
            )
        key = str(key_node.value)
        if key not in known:
            continue
        if key in fields:
            raise AgentDefinitionRefused(AgentDefinitionRefusal.FIELD_DUPLICATED, key)
        fields[key] = value_node
    return fields


def _authored_text(node: Node, field: AgentDefinitionField) -> str:
    if not isinstance(node, ScalarNode) or node.tag != _YAML_TEXT_TAG:
        raise AgentDefinitionRefused(
            AgentDefinitionRefusal.FIELD_TYPE_UNEXPECTED, field.value
        )
    return str(node.value)


def _authored_model(fields: dict[str, Node]) -> str | None:
    node = fields.get(AgentDefinitionField.MODEL.value)
    if node is None:
        return None
    return _authored_text(node, AgentDefinitionField.MODEL)


def _tool_declaration(fields: dict[str, Node]) -> AgentToolDeclaration:
    node = fields.get(AgentDefinitionField.TOOLS.value)
    if node is None:
        return UnrestrictedTools()
    if isinstance(node, SequenceNode):
        return DeclaredTools(
            tuple(
                AgentToolName(_authored_text(entry, AgentDefinitionField.TOOLS))
                for entry in node.value
            )
        )
    declared = _authored_text(node, AgentDefinitionField.TOOLS).strip()
    if not declared:
        return DeclaredTools(())
    # The separator's surrounding whitespace belongs to the spelling, not to any
    # tool name the author meant.
    return DeclaredTools(
        tuple(AgentToolName(name.strip()) for name in declared.split(TOOL_SEPARATOR))
    )
