"""The MCP tools, named once, each bound to the HTTP doors they call.

This is the one owner of the tool names and of the input schemas a client sees.
The schemas are derived from the wire models those doors already publish: a
second handwritten grammar here is how MCP and HTTP would drift. The published
workflow-document grammar is referenced from `start_run`, not copied.
`publish_artifact` is the JSON encoding of an octet-stream door: MCP cannot
carry those bytes except as Base64 -- unless the material is already a file on
this machine, which this child reads itself.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter

from atelier2.api.openapi import (
    API_PREFIX,
    ARTIFACT_PATH,
    ARTIFACTS_PATH,
    CATALOG_REVISION_BY_NAME_PATH,
    WORKFLOW_DOCUMENT_COMPONENT,
)
from atelier2.api.wire.requests import (
    AnswerWaitRequestResource,
    ArtifactOrderResource,
    StartRunAgentBindingResourceV2,
    WorkItemOrderResource,
)
from atelier2.api.wire.resources import (
    ArtifactResource,
    CatalogNameResolutionResource,
    ProblemResource,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.host.run_command import DEFAULT_CATALOG_POSITION

_START_RUN_ORDER = TypeAdapter(ArtifactOrderResource | WorkItemOrderResource)

MCP_SERVER_NAME = "atelier2"
MCP_SERVER_VERSION = "0.0.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
JSONRPC_VERSION = "2.0"
MAXIMUM_MCP_INPUT_LINE_BYTES = 1_048_576
"""The largest JSON-RPC request body this stdio door reads, excluding its newline."""

MCP_PUBLISH_ARTIFACT_REQUEST_ENVELOPE_RESERVATION_BYTES = 1_024
"""Space reserved for the JSON-RPC request around Base64 artifact material."""

MAXIMUM_MCP_ARTIFACT_BASE64_CHARACTERS = 4 * (
    (
        MAXIMUM_MCP_INPUT_LINE_BYTES
        - MCP_PUBLISH_ARTIFACT_REQUEST_ENVELOPE_RESERVATION_BYTES
    )
    // 4
)
"""Standard Base64 characters that fit beside the reserved request envelope."""

MAXIMUM_MCP_ARTIFACT_BYTES = min(
    MAXIMUM_ARTIFACT_BYTES,
    MAXIMUM_MCP_ARTIFACT_BASE64_CHARACTERS * 3 // 4,
)
"""Exact decoded bytes whose Base64 fits the reserved `publish_artifact` request."""

METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"
METHOD_PING = "ping"


class McpToolName(StrEnum):
    """The six tools this door exposes, in the order a client lists them."""

    LIST_WORKFLOWS = "list_workflows"
    START_RUN = "start_run"
    RUN_STATUS = "run_status"
    ANSWER_WAIT = "answer_wait"
    PUBLISH_ARTIFACT = "publish_artifact"
    READ_ARTIFACT = "read_artifact"


class McpRefusal(StrEnum):
    """Every refusal this adapter decides for itself, named once.

    The service's own refusals travel as its typed problem; these are the ones
    it never gets to make -- material that cannot fit the transport, an order
    outside this door's subset, a local path this child will not read, and an
    answer that is not the material the address asked for.
    """

    ARTIFACT_PAYLOAD_TOO_LARGE = "mcp-artifact-payload-too-large"
    ARTIFACT_CONTENT_NOT_BASE64 = "mcp-artifact-content-not-base64"
    ARTIFACT_ANSWER_TOO_LARGE = "mcp-artifact-answer-too-large"
    ARTIFACT_ANSWER_NOT_ITS_ADDRESS = "mcp-artifact-answer-not-its-address"
    ARTIFACT_SOURCE_AMBIGUOUS = "mcp-artifact-source-ambiguous"
    ARTIFACT_PATH_NOT_ABSOLUTE = "mcp-artifact-path-not-absolute"
    ARTIFACT_PATH_NOT_FOUND = "mcp-artifact-path-not-found"
    ARTIFACT_PATH_NOT_A_REGULAR_FILE = "mcp-artifact-path-not-a-regular-file"
    ARTIFACT_PATH_UNREADABLE = "mcp-artifact-path-unreadable"
    ARTIFACT_PATH_TOO_LARGE = "mcp-artifact-path-too-large"
    START_RUN_ORDER_INVALID = "mcp-start-run-order-invalid"


ARTIFACT_CONTENT_BASE64_FIELD = "content_base64"
"""The one name Base64 artifact material travels under, in both directions."""

ARTIFACT_PATH_FIELD = "path"
"""The one name a local file travels under into `publish_artifact`."""

ARTIFACT_HASH_FIELD = "artifact_hash"
"""The address field, spelled exactly as the published artifact resource does."""

_JSON_SCHEMA_DEFS_KEY = "$defs"


McpHttpDoor = tuple[str, str]


MCP_TOOL_HTTP_DOORS: dict[McpToolName, tuple[McpHttpDoor, ...]] = {
    McpToolName.LIST_WORKFLOWS: (
        ("get", API_PREFIX + "/workflow-revisions"),
        ("get", CATALOG_REVISION_BY_NAME_PATH),
    ),
    McpToolName.START_RUN: (
        ("get", CATALOG_REVISION_BY_NAME_PATH),
        ("post", API_PREFIX + "/runs"),
    ),
    McpToolName.RUN_STATUS: (("get", API_PREFIX + "/runs/{public_ref}"),),
    McpToolName.ANSWER_WAIT: (("post", API_PREFIX + "/runs/{public_ref}/answers"),),
    McpToolName.PUBLISH_ARTIFACT: (("post", ARTIFACTS_PATH),),
    McpToolName.READ_ARTIFACT: (("get", ARTIFACT_PATH),),
}


def tool_definitions() -> tuple[dict[str, Any], ...]:
    """The list `tools/list` answers with, in the order the desk named."""

    return (
        {
            "name": McpToolName.LIST_WORKFLOWS.value,
            "description": (
                "List catalog workflows as name, lineage and head. Composed from "
                "GET /workflow-revisions?view=described and GET "
                "/catalog-revisions/by-name/{kind}/{name}. Unadmitted published titles "
                "are omitted. There is no caller authentication on this API: the "
                "stdio child has the same loopback trust as the browser. #82 is "
                "human OIDC and is not this door; machine credentials wait on "
                "ADR 0009."
            ),
            "inputSchema": _object_schema({}),
        },
        {
            "name": McpToolName.START_RUN.value,
            "description": (
                "Start the revision a catalog name holds, using the same "
                "resolution `atelier2 run --name` asks, then POST /runs. "
                f"`orders` use the artifact and work-item forms of the published "
                f"{WORKFLOW_DOCUMENT_COMPONENT} grammar; inline material is not "
                "an MCP order. Publish material first, then name its returned "
                "hash here: those are two calls. A failed or refused start leaves that "
                "immutable artifact reusable and creates no run. `run_id` is "
                "required so a repeating agent cannot mint a second billed run. "
                "Times on the run wait for #355. No caller authentication exists; "
                "do not invent one."
            ),
            "inputSchema": _start_run_input_schema(),
        },
        {
            "name": McpToolName.RUN_STATUS.value,
            "description": (
                "Read one run through GET /runs/{public_ref}. The body is the "
                "API resource: state, terminal hash when the run has ended, and "
                "the rail. `answerable_wait` is this adapter's exact actor and "
                "execution fence for an open wait, or null. The service's typed problem is "
                "returned unchanged when the run is missing."
            ),
            "inputSchema": _object_schema(
                {
                    "public_run_reference": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The public run reference POST /runs answered.",
                    }
                },
                required=("public_run_reference",),
            ),
        },
        {
            "name": McpToolName.ANSWER_WAIT.value,
            "description": (
                "Answer a waiting run through POST /runs/{public_ref}/answers, "
                "the same door #194 already proved. The body fields are the "
                "wire resource's own. A V3 wait admits what its published "
                "output schema admits; a V1/V2 wait still admits canonical "
                "integer bytes. The service's ApiProblem is returned unchanged."
            ),
            "inputSchema": _answer_wait_input_schema(),
        },
        {
            "name": McpToolName.PUBLISH_ARTIFACT.value,
            "description": (
                "Publish exact bytes through POST /artifacts as "
                "application/octet-stream. Name exactly one source. Use "
                f"{ARTIFACT_PATH_FIELD} when the material is already a file on "
                "this machine -- this child runs beside the service, reads the "
                "file itself and sends the bytes, so nothing large has to be "
                f"reproduced inside a tool call. Use {ARTIFACT_CONTENT_BASE64_FIELD} "
                "only for material you are composing in the call itself, because "
                "MCP JSON cannot carry octet-stream and the HTTP door is not "
                "JSON. Naming both or neither is refused as "
                f"{McpRefusal.ARTIFACT_SOURCE_AMBIGUOUS.value}. The answer is "
                "ArtifactResource. Publishing the same bytes again is the same "
                "artifact. Publish then start are two calls: a failed or refused start leaves "
                "the immutable artifact reusable and creates no run. Empty material "
                f"is the service's typed problem. {ARTIFACT_CONTENT_BASE64_FIELD} "
                f"accepts at most {MAXIMUM_MCP_ARTIFACT_BYTES} decoded bytes "
                f"({MAXIMUM_MCP_ARTIFACT_BASE64_CHARACTERS} Base64 characters): "
                "Base64 plus the "
                f"{MCP_PUBLISH_ARTIFACT_REQUEST_ENVELOPE_RESERVATION_BYTES}-byte "
                "JSON-RPC envelope reservation must fit the "
                f"{MAXIMUM_MCP_INPUT_LINE_BYTES}-byte line cap. Larger content is "
                "refused locally as "
                f"{McpRefusal.ARTIFACT_PAYLOAD_TOO_LARGE.value} and is not sent. "
                f"{ARTIFACT_PATH_FIELD} carries the whole store bound of "
                f"{MAXIMUM_ARTIFACT_BYTES} bytes, and a path that is not "
                "absolute, is missing, is not a regular file, cannot be read, or "
                "exceeds that bound is refused before anything is sent. No caller "
                "authentication exists; do not invent one."
            ),
            "inputSchema": {
                **_object_schema(
                    {
                        ARTIFACT_CONTENT_BASE64_FIELD: {
                            "type": "string",
                            "maxLength": MAXIMUM_MCP_ARTIFACT_BASE64_CHARACTERS,
                            "description": (
                                "Standard Base64 of the exact bytes to POST as "
                                "application/octet-stream, up to "
                                f"{MAXIMUM_MCP_ARTIFACT_BYTES} decoded bytes "
                                f"({MAXIMUM_MCP_ARTIFACT_BASE64_CHARACTERS} "
                                "characters)."
                            ),
                        },
                        ARTIFACT_PATH_FIELD: {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Absolute path of a regular file on the machine this "
                                "child runs on, at most "
                                f"{MAXIMUM_ARTIFACT_BYTES} bytes. Its exact bytes are "
                                "read here and POSTed unchanged; a symbolic link is "
                                "read through to the file it names."
                            ),
                        },
                    }
                ),
                "oneOf": [
                    {"required": [ARTIFACT_CONTENT_BASE64_FIELD]},
                    {"required": [ARTIFACT_PATH_FIELD]},
                ],
            },
        },
        {
            "name": McpToolName.READ_ARTIFACT.value,
            "description": (
                "Read published material back through GET /artifacts/"
                "{artifact_hash}, the read side of the door publish_artifact "
                "wrote to. The answer carries the address and the exact bytes as "
                f"{ARTIFACT_CONTENT_BASE64_FIELD}; decode it to get the file "
                "back. This is how a run's terminal material, or anything a "
                "hash names, is read without replaying an event stream. An "
                "unknown address is the service's typed problem, and bytes that "
                "do not hash to the address that was asked for are refused as "
                f"{McpRefusal.ARTIFACT_ANSWER_NOT_ITS_ADDRESS.value}. Bytes above "
                f"{MAXIMUM_MCP_ARTIFACT_BYTES} do not fit one JSON-RPC line and "
                f"are refused as {McpRefusal.ARTIFACT_ANSWER_TOO_LARGE.value}."
            ),
            "inputSchema": _object_schema(
                {
                    ARTIFACT_HASH_FIELD: _published_artifact_hash_schema(),
                },
                required=(ARTIFACT_HASH_FIELD,),
            ),
        },
    )


def _published_artifact_hash_schema() -> dict[str, Any]:
    """The address field exactly as the published artifact resource spells it."""

    published = ArtifactResource.model_json_schema()["properties"][ARTIFACT_HASH_FIELD]
    return {
        **published,
        "description": "The artifact hash a publication or a run answered with.",
    }


def _start_run_input_schema() -> dict[str, Any]:
    """Name plus the MCP subset of the start body POST /runs already owns."""

    order_schema = dict(_START_RUN_ORDER.json_schema())
    order_defs = order_schema.pop(_JSON_SCHEMA_DEFS_KEY, {})
    schema = _object_schema(
        {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The catalog name to resolve. The service decides which "
                    "revision it holds."
                ),
            },
            "position": {
                "type": "string",
                "default": DEFAULT_CATALOG_POSITION,
                "description": (
                    "Which member of the lineage to start: head, or an exact "
                    f"member number (default {DEFAULT_CATALOG_POSITION})."
                ),
            },
            "run_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "This run's own identity. Repeating the same identity "
                    "reports the first run instead of starting another."
                ),
            },
            "agent_bindings": {
                "type": "array",
                "description": (
                    "Already-published configuration hashes, one per role. "
                    "The shape is the start request's own."
                ),
                "items": StartRunAgentBindingResourceV2.model_json_schema(),
            },
            "orders": {
                "type": "array",
                "description": (
                    "Artifact hashes already returned by publish_artifact, or "
                    "work-item references. These are the published "
                    f"{WORKFLOW_DOCUMENT_COMPONENT} grammar's MCP order forms; "
                    "inline {name, value} material is refused."
                ),
                "items": order_schema,
            },
        },
        required=("name", "run_id"),
    )
    if order_defs:
        schema[_JSON_SCHEMA_DEFS_KEY] = order_defs
    return schema


def _answer_wait_input_schema() -> dict[str, Any]:
    answered = AnswerWaitRequestResource.model_json_schema()
    properties = {
        "public_run_reference": {
            "type": "string",
            "minLength": 1,
            "description": "The public run reference of the waiting run.",
        },
        **answered.get("properties", {}),
    }
    required = ("public_run_reference", *answered.get("required", ()))
    schema = _object_schema(properties, required=tuple(required))
    if _JSON_SCHEMA_DEFS_KEY in answered:
        schema[_JSON_SCHEMA_DEFS_KEY] = answered[_JSON_SCHEMA_DEFS_KEY]
    return schema


def _object_schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


def catalog_listing_resource(
    items: tuple[CatalogNameResolutionResource, ...],
) -> dict[str, Any]:
    """The list tool's success body: the catalog resolutions, not a new vocabulary."""

    return {"items": [item.model_dump(mode="json") for item in items]}


def artifact_content_answer(artifact_hash: str, content: bytes) -> dict[str, Any]:
    """The read tool's success body: the published address plus its exact bytes.

    The address is spelled by the resource the publication already answers
    with, so both directions of this door name it identically.
    """

    return {
        **ArtifactResource(artifact_hash=artifact_hash).model_dump(mode="json"),
        ARTIFACT_CONTENT_BASE64_FIELD: base64.standard_b64encode(content).decode(
            "ascii"
        ),
    }


def problem_payload(problem: ProblemResource) -> dict[str, Any]:
    """The refusal a tool returns: the API problem, field pointers included."""

    return problem.model_dump(mode="json", exclude_none=True)
