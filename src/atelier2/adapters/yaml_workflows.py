from __future__ import annotations

import codecs
from collections.abc import Iterable, Mapping
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode
from yaml.tokens import (
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    DirectiveToken,
    DocumentStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    StreamStartToken,
    TagToken,
    Token,
)

from atelier2.contracts.workflow_documents import (
    WORKFLOW_DOCUMENT_FORMATS,
    WorkflowDocumentFormat,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflow_refusals import (
    WorkflowDocumentInvalid,
    WorkflowDocumentRefused,
    WorkflowRefusal,
    WorkflowRefusalReason,
)
from atelier2.contracts.workflows_v3 import (
    AnyWorkflowDocument,
    WorkflowGraphV3,
    what_a_v3_document_still_waits_for,
)

FORMAT_V3_NOT_EXECUTABLE = (
    "workflow format version 3 parses, but no runtime executes these node kinds"
)

MAXIMUM_DOCUMENT_DEPTH = 32
"""How deep a workflow document may nest, well above the deepest authored form.

The composer recurses per nesting level, so an unbounded document exhausts the
interpreter stack instead of being refused; this bound is what keeps the refusal
a named one.
"""

MERGE_KEY = "<<"
DOCUMENT_START = "---"

_FORBIDDEN_YAML_TOKENS: Mapping[type[Token], str] = {
    AliasToken: "alias",
    AnchorToken: "anchor",
    TagToken: "tag",
}
_NESTING_START_TOKENS = (
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingStartToken,
    FlowSequenceStartToken,
)
_NESTING_END_TOKENS = (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)


class InvalidWorkflowDocument(WorkflowDocumentInvalid):
    """The exact workflow bytes are not one safe, closed YAML graph."""


class WorkflowFormatNotExecutable(InvalidWorkflowDocument):
    """The document is valid, and no runtime here executes its format version."""


def _refused(
    reason: WorkflowRefusalReason, field: str, detail: str
) -> InvalidWorkflowDocument:
    """One document-level refusal that keeps its reason and subject."""
    refusal = WorkflowRefusal(reason, field, detail)
    return InvalidWorkflowDocument(str(refusal), refusal)


class StrictWorkflowLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise _refused(
                WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
                "alias",
                "aliases are forbidden",
            )
        return super().compose_node(parent, index)

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise _refused(
                WorkflowRefusalReason.MALFORMED_DOCUMENT,
                "syntax",
                "expected a mapping",
            )
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge" or key_node.value == MERGE_KEY:
                raise _refused(
                    WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
                    MERGE_KEY,
                    "merge keys are forbidden",
                )
            key = self.construct_object(key_node, deep=False)
            try:
                duplicate = key in seen
            except TypeError as error:
                raise _refused(
                    WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
                    "key",
                    "mapping keys must be scalar",
                ) from error
            if duplicate:
                raise _refused(
                    WorkflowRefusalReason.DUPLICATE_KEY,
                    str(key),
                    f"the key {key!r} is declared twice",
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_workflow_document(document: bytes) -> AnyWorkflowDocument:
    try:
        decoded = _decoded_text(document)
        _refuse_unsafe_yaml_form(yaml.scan(decoded))
        loaded = yaml.load(decoded, Loader=StrictWorkflowLoader)
        if loaded is None:
            raise _refused(
                WorkflowRefusalReason.MALFORMED_DOCUMENT,
                "document",
                "workflow document is empty",
            )
        _freeze_node_sequences(loaded)
        return _document_format_of(loaded).read(loaded)
    except InvalidWorkflowDocument:
        raise
    except WorkflowDocumentRefused as refused:
        raise InvalidWorkflowDocument(str(refused), refused.refusal) from refused
    except (yaml.YAMLError, ValidationError, TypeError, ValueError) as error:
        raise _refused(
            WorkflowRefusalReason.MALFORMED_DOCUMENT,
            "syntax",
            "workflow document violates safe YAML v1",
        ) from error


def _freeze_node_sequences(loaded: object) -> None:
    if not isinstance(loaded, dict) or not isinstance(loaded.get("nodes"), list):
        return
    nodes = loaded["nodes"]
    for node in nodes:
        if isinstance(node, dict) and isinstance(node.get("operands"), list):
            node["operands"] = tuple(node["operands"])
    loaded["nodes"] = tuple(nodes)


def _document_format_of(loaded: object) -> WorkflowDocumentFormat:
    if not isinstance(loaded, dict) or type(loaded.get("format_version")) is not int:
        raise _refused(
            WorkflowRefusalReason.INVALID_VALUE,
            "format_version",
            "workflow format version must be a strict integer",
        )
    declared = loaded["format_version"]
    try:
        version = WorkflowFormatVersion(declared)
    except ValueError:
        version = None
    document_format = (
        None if version is None else WORKFLOW_DOCUMENT_FORMATS.get(version)
    )
    if document_format is None:
        raise _refused(
            WorkflowRefusalReason.INVALID_VALUE,
            "format_version",
            f"workflow format version {declared} is unsupported",
        )
    return document_format


def _decoded_text(document: bytes) -> str:
    if not document:
        raise _refused(
            WorkflowRefusalReason.MALFORMED_DOCUMENT,
            "document",
            "workflow document is empty",
        )
    if document.startswith(codecs.BOM_UTF8):
        raise _refused(
            WorkflowRefusalReason.MALFORMED_DOCUMENT,
            "encoding",
            "workflow document must not carry a byte order mark",
        )
    try:
        return document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise _refused(
            WorkflowRefusalReason.MALFORMED_DOCUMENT,
            "encoding",
            "workflow document must be UTF-8",
        ) from error


def _refuse_unsafe_yaml_form(tokens: Iterable[Token]) -> None:
    """Refuse every unsafe YAML form by name, before anything is composed."""
    depth = 0
    content_scanned = False
    for token in tokens:
        feature = _FORBIDDEN_YAML_TOKENS.get(type(token))
        if feature is not None:
            raise _refused(
                WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
                feature,
                "anchors, aliases, and explicit tags are forbidden",
            )
        if isinstance(token, DocumentStartToken):
            if content_scanned:
                raise _refused(
                    WorkflowRefusalReason.MULTIPLE_DOCUMENTS,
                    DOCUMENT_START,
                    "a workflow revision is one YAML document",
                )
        elif not isinstance(token, (StreamStartToken, DirectiveToken)):
            content_scanned = True
        if isinstance(token, _NESTING_START_TOKENS):
            depth += 1
            if depth > MAXIMUM_DOCUMENT_DEPTH:
                raise _refused(
                    WorkflowRefusalReason.DOCUMENT_TOO_DEEP,
                    "nesting",
                    f"workflow document nests deeper than {MAXIMUM_DOCUMENT_DEPTH} "
                    "levels",
                )
        elif isinstance(token, _NESTING_END_TOKENS):
            depth -= 1


def parse_executable_workflow_document(document: bytes) -> AnyWorkflowDocument:
    """Parse one document the current runtime can execute, refusing what it cannot.

    A V3 document is executable exactly as far as its node kinds are interpreted.
    Today those are the Agent kind, on the attempt path V2 already owns, and the
    Wait kind, on the pause and answer path V2 already owns; every other kind is
    refused by name rather than started and abandoned at the first node no runtime
    can run. The refusal names the kind, so an author learns which one is waiting
    rather than that "version 3" is.
    """
    graph = parse_workflow_document(document)
    if isinstance(graph, WorkflowGraphV3):
        waiting = what_a_v3_document_still_waits_for(graph)
        if waiting is not None:
            raise WorkflowFormatNotExecutable(f"{FORMAT_V3_NOT_EXECUTABLE}: {waiting}")
    return graph
