"""stdio MCP server: five tools, each a client of the public HTTP API.

This module is a third door onto the same product, not a second way into it.
Every effect travels through the published HTTP API of a service someone is
already serving. There is no listener, no port, no token, and no caller
identity: the API has no authentication concept for callers, #82 is human
OIDC, and ADR 0009 (machine credentials) is not landed. The child therefore
has the same trust as the browser on loopback, and it refuses any other
service address rather than inventing a credential.

The official MCP SDK is a multi-transport server (SSE, HTTP sessions,
resources, prompts). This slice is stdio JSON-RPC for six tools. A
dependency would add a second HTTP stack beside FastAPI without removing a
hard problem this door has. Protocol tokens live with the tools; the
newline-delimited JSON-RPC line protocol lives here.

A billed executor composed on the service is the item's stop condition for
any future non-loopback exposure. This process cannot listen, so that
exposure is not this slice.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import IO, Any, assert_never
from urllib.parse import quote, urlencode, urlsplit

from pydantic import TypeAdapter, ValidationError

from atelier2.api.wire.requests import (
    ArtifactOrderResource,
    RevisionListingView,
    StartRunAgentBindingResourceV2,
    WorkItemOrderResource,
)
from atelier2.api.wire.resources import (
    ArtifactResource,
    CatalogNameResolutionResource,
    RunResourceV3,
    VersionedWorkflowRevisionPageResource,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES, ArtifactHash
from atelier2.contracts.executions import WaitAnswerActor
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.host.address import (
    ADDRESSABLE_SCHEMES,
    DEFAULT_SERVICE_URL,
    is_loopback_service_url,
)
from atelier2.host.atelier_api_client import AtelierApi, AtelierApiTransportFailure
from atelier2.host.mcp_tools import (
    ARTIFACT_CONTENT_BASE64_FIELD,
    ARTIFACT_HASH_FIELD,
    ARTIFACT_PATH_FIELD,
    JSONRPC_VERSION,
    MAXIMUM_MCP_ARTIFACT_BYTES,
    MAXIMUM_MCP_INPUT_LINE_BYTES,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    METHOD_INITIALIZE,
    METHOD_PING,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    McpRefusal,
    McpToolName,
    artifact_content_answer,
    catalog_listing_resource,
    problem_payload,
    tool_definitions,
)
from atelier2.host.run_command import (
    ARTIFACT_PATH,
    DEFAULT_CATALOG_POSITION,
    JSON_MEDIA_TYPE,
    OCTET_STREAM_MEDIA_TYPE,
    REQUEST_TIMEOUT_SECONDS,
    RUN_PATH,
    WORKFLOW_REVISION_PATH,
    AgentRoleBinding,
    NameOrder,
    ServiceRefused,
    ServiceUnreachable,
    SuppliedArtifactOrder,
    SuppliedStartOrder,
    SuppliedWorkItemOrder,
    UnreadableServiceAnswer,
    UnusableRunOrder,
    _refused,
    catalog_name_path,
    decoded,
    resolve_published_name,
    service_api,
    start_request_body,
)

_run_resource = TypeAdapter[RunResourceV3](RunResourceV3)
_catalog_name_resolution = TypeAdapter(CatalogNameResolutionResource)
_described_page = TypeAdapter(VersionedWorkflowRevisionPageResource)
_artifact_resource = TypeAdapter(ArtifactResource)
_start_run_order = TypeAdapter(ArtifactOrderResource | WorkItemOrderResource)

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602

ToolHandler = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class McpServiceRefusal(Exception):
    """The operator named a service this process will not speak to."""


class McpLocalRefusal(UnusableRunOrder):
    """A refusal this child decides itself, before any service is asked.

    The refusal is named by the adapter's own vocabulary rather than spelled
    into a message, so a caller reads the same token the tool text promised.
    """

    def __init__(self, refusal: McpRefusal, detail: str | None = None) -> None:
        super().__init__(
            refusal.value if detail is None else f"{refusal.value}: {detail}"
        )
        self.refusal = refusal


@dataclass(frozen=True)
class _JsonRpcRequest:
    message_id: object
    method: str
    params: Mapping[str, Any]
    notification: bool = False


def execute_mcp(service_url: str, stdin: IO[bytes], stdout: IO[bytes]) -> int:
    """Speak MCP on the given streams against one loopback service."""

    try:
        _require_loopback_service(service_url)
    except McpServiceRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    serve_mcp(service_url, stdin, stdout)
    return 0


def serve_mcp(service_url: str, stdin: IO[bytes], stdout: IO[bytes]) -> None:
    """Read newline-delimited JSON-RPC from stdin until it closes. Write each reply."""

    while True:
        try:
            raw = read_message(stdin)
        except UnreadableServiceAnswer as refusal:
            write_message(stdout, _error(None, JSONRPC_PARSE_ERROR, str(refusal)))
            continue
        if raw is None:
            return
        reply = dispatch_message(service_url, raw)
        if reply is not None:
            write_message(stdout, reply)


def dispatch_message(service_url: str, raw: object) -> dict[str, Any] | None:
    """One JSON-RPC message to at most one reply. Notifications return None."""

    parsed = _jsonrpc_request(raw)
    if isinstance(parsed, dict):
        return parsed
    if parsed.notification:
        return None
    if parsed.method == METHOD_INITIALIZE:
        return _result(parsed.message_id, _initialize_result())
    if parsed.method == METHOD_PING:
        return _result(parsed.message_id, {})
    if parsed.method == METHOD_TOOLS_LIST:
        return _result(parsed.message_id, {"tools": list(tool_definitions())})
    if parsed.method == METHOD_TOOLS_CALL:
        return _result(parsed.message_id, _call_tool(service_url, parsed.params))
    return _error(
        parsed.message_id,
        JSONRPC_METHOD_NOT_FOUND,
        f"method {parsed.method!r} is not this server",
    )


def read_message(stream: IO[bytes]) -> object | None:
    """One newline-delimited JSON-RPC message, or None at end of input."""

    line = stream.readline(MAXIMUM_MCP_INPUT_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAXIMUM_MCP_INPUT_LINE_BYTES and not line.endswith(b"\n"):
        raise UnreadableServiceAnswer("the MCP line is larger than this server reads")
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise UnreadableServiceAnswer(f"the MCP line is not JSON: {error}") from error


def write_message(stream: IO[bytes], message: Mapping[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    stream.write(body + b"\n")
    stream.flush()


def _jsonrpc_request(raw: object) -> _JsonRpcRequest | dict[str, Any]:
    if not isinstance(raw, dict):
        return _error(None, JSONRPC_INVALID_REQUEST, "a JSON-RPC message is an object")
    if "id" not in raw:
        method = raw.get("method")
        params = raw.get("params") or {}
        return _JsonRpcRequest(
            None,
            method if isinstance(method, str) else "",
            params if isinstance(params, dict) else {},
            notification=True,
        )
    message_id = raw.get("id")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        return _error(message_id, JSONRPC_INVALID_REQUEST, "jsonrpc must be 2.0")
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        return _error(message_id, JSONRPC_INVALID_REQUEST, "method must be a string")
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(message_id, JSONRPC_INVALID_PARAMS, "params must be an object")
    return _JsonRpcRequest(message_id, method, params)


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
    }


def _call_tool(service_url: str, params: Mapping[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not name:
        return _tool_error({"error": "tools/call requires a name"})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _tool_error({"error": "tools/call arguments must be an object"})
    try:
        tool = McpToolName(name)
    except ValueError:
        return _tool_error({"error": f"unknown tool {name!r}"})
    try:
        payload = _tool_handlers()[tool](service_url, arguments)
    except ServiceRefused as refused:
        if refused.problem is None:
            return _tool_error({"error": str(refused)})
        return _tool_error(problem_payload(refused.problem))
    except (ServiceUnreachable, UnreadableServiceAnswer, UnusableRunOrder) as refusal:
        return _tool_error({"error": str(refusal)})
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": False,
    }


def _tool_handlers() -> dict[McpToolName, ToolHandler]:
    return {
        McpToolName.LIST_WORKFLOWS: list_workflows,
        McpToolName.START_RUN: start_run,
        McpToolName.RUN_STATUS: run_status,
        McpToolName.ANSWER_WAIT: answer_wait,
        McpToolName.PUBLISH_ARTIFACT: publish_artifact,
        McpToolName.READ_ARTIFACT: read_artifact,
    }


def list_workflows(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    del arguments
    api = service_api(service_url)
    items: list[CatalogNameResolutionResource] = []
    seen: set[str] = set()
    after: str | None = None
    while True:
        query = {"view": RevisionListingView.DESCRIBED.value}
        if after is not None:
            query["after_revision_hash"] = after
        page = decoded(
            _described_page,
            _get(api, f"{WORKFLOW_REVISION_PATH}?{urlencode(query)}"),
            "a described revision page",
        )
        for revision in page.items:
            name = revision.name
            if name is None or name in seen:
                continue
            seen.add(name)
            resolved = _resolve_listed_name(api, name)
            if resolved is not None:
                items.append(resolved)
        if page.next_after_revision_hash is None:
            break
        after = page.next_after_revision_hash
    return catalog_listing_resource(tuple(items))


def start_run(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    name = _required_text(arguments, "name")
    run_id = _required_text(arguments, "run_id")
    position = arguments.get("position", DEFAULT_CATALOG_POSITION)
    if not isinstance(position, str) or not position:
        raise UnusableRunOrder("position must be a nonempty string")
    bindings = _bindings(arguments.get("agent_bindings"))
    orders = _orders(arguments.get("orders"))
    resolution = resolve_published_name(NameOrder(service_url, name, position))
    started = decoded(
        _run_resource,
        _post(
            service_api(service_url),
            RUN_PATH,
            start_request_body(
                run_id,
                resolution.revision_hash,
                tuple(
                    AgentRoleBinding(
                        binding.role, binding.agent_configuration_revision_hash
                    )
                    for binding in bindings
                ),
                orders,
            ),
        ),
        "a run",
    )
    return started.model_dump(mode="json")


def run_status(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    reference = _required_text(arguments, "public_run_reference")
    ended = decoded(
        _run_resource,
        _get(service_api(service_url), f"{RUN_PATH}/{quote(reference, safe='')}"),
        "a run",
    )
    resource = ended.model_dump(mode="json")
    resource["answerable_wait"] = _answerable_wait(ended)
    return resource


def _answerable_wait(run: RunResourceV3) -> dict[str, str] | None:
    """The exact fence MCP can write back to the answer door, or no open wait."""

    if not isinstance(run, RunResourceV3) or run.state != "WAITING_INPUT":
        return None
    execution_id = run.current_node_execution_id
    cancellation_target = run.cancellation.target_node_execution_id
    if cancellation_target is None or cancellation_target != execution_id:
        raise UnreadableServiceAnswer(
            "the service answered a waiting run whose execution fences disagree"
        )
    return {
        "actor": WaitAnswerActor.OPERATOR.value,
        "expected_node_execution_id": execution_id,
    }


def answer_wait(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    reference = _required_text(arguments, "public_run_reference")
    workflow_revision_hash = _required_text(arguments, "workflow_revision_hash")
    node_id = _required_text(arguments, "node_id")
    expected_node_execution_id = _required_text(arguments, "expected_node_execution_id")
    actor = _required_text(arguments, "actor")
    answer_base64 = _required_text(arguments, "answer_base64")
    answered = decoded(
        _run_resource,
        _post(
            service_api(service_url),
            f"{RUN_PATH}/{quote(reference, safe='')}/answers",
            json.dumps(
                {
                    "workflow_revision_hash": workflow_revision_hash,
                    "node_id": node_id,
                    "expected_node_execution_id": expected_node_execution_id,
                    "actor": actor,
                    "answer_base64": answer_base64,
                }
            ).encode(),
        ),
        "a run",
    )
    return answered.model_dump(mode="json")


def publish_artifact(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    content = _artifact_content(arguments)
    published = decoded(
        _artifact_resource,
        _post(
            service_api(service_url),
            ARTIFACT_PATH,
            content,
            media_type=OCTET_STREAM_MEDIA_TYPE,
        ),
        "an artifact",
    )
    return published.model_dump(mode="json")


def read_artifact(service_url: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    asked = _required_text(arguments, ARTIFACT_HASH_FIELD)
    content = _get(
        service_api(service_url),
        f"{ARTIFACT_PATH}/{quote(asked, safe='')}",
        accept=OCTET_STREAM_MEDIA_TYPE,
    )
    if ArtifactHash.of(content).value != asked:
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_ANSWER_NOT_ITS_ADDRESS,
            f"the service answered bytes that do not hash to {asked}",
        )
    if len(content) > MAXIMUM_MCP_ARTIFACT_BYTES:
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_ANSWER_TOO_LARGE,
            f"{len(content)} bytes exceeds {MAXIMUM_MCP_ARTIFACT_BYTES}",
        )
    return artifact_content_answer(asked, content)


def _resolve_listed_name(
    api: AtelierApi, name: str
) -> CatalogNameResolutionResource | None:
    """Resolve one listed title. An unadmitted name is not a catalog member."""

    asked = catalog_name_path(RevisionKind.WORKFLOW, name)
    try:
        return decoded(_catalog_name_resolution, _get(api, asked), "a catalog name")
    except ServiceRefused as refused:
        if refused.problem is not None and refused.problem.type.endswith(
            "catalog-name-not-found"
        ):
            return None
        raise


def _bindings(raw: object) -> tuple[StartRunAgentBindingResourceV2, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise UnusableRunOrder("agent_bindings must be an array")
    try:
        return tuple(
            StartRunAgentBindingResourceV2.model_validate(item) for item in raw
        )
    except ValidationError as error:
        raise UnusableRunOrder(
            f"agent_bindings is not the start request's binding shape: {error}"
        ) from error


def _orders(raw: object) -> tuple[SuppliedStartOrder, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _start_run_order_refusal()
    try:
        return tuple(
            _supplied_start_order(_start_run_order.validate_python(item))
            for item in raw
        )
    except ValidationError as error:
        raise _start_run_order_refusal() from error


def _supplied_start_order(
    order: ArtifactOrderResource | WorkItemOrderResource,
) -> SuppliedStartOrder:
    match order:
        case ArtifactOrderResource(name=name, artifact_hash=artifact_hash):
            return SuppliedArtifactOrder(name, artifact_hash)
        case WorkItemOrderResource(name=name, work_item=work_item):
            return SuppliedWorkItemOrder(name, work_item)
        case _ as unreachable:
            assert_never(unreachable)


def _start_run_order_refusal() -> McpLocalRefusal:
    return McpLocalRefusal(
        McpRefusal.START_RUN_ORDER_INVALID,
        "each order must be exactly {name, artifact_hash} or {name, work_item}",
    )


def _artifact_content(arguments: Mapping[str, Any]) -> bytes:
    """The exact bytes to publish, from the one source this call named."""

    inline = arguments.get(ARTIFACT_CONTENT_BASE64_FIELD)
    named_path = arguments.get(ARTIFACT_PATH_FIELD)
    if (inline is None) == (named_path is None):
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_SOURCE_AMBIGUOUS,
            f"name exactly one of {ARTIFACT_CONTENT_BASE64_FIELD} or "
            f"{ARTIFACT_PATH_FIELD}",
        )
    if named_path is not None:
        return _artifact_file_bytes(named_path)
    return _artifact_bytes(inline)


def _artifact_bytes(raw: object) -> bytes:
    # MCP JSON cannot carry octet-stream; the HTTP door still takes the bytes.
    if not isinstance(raw, str):
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_CONTENT_NOT_BASE64,
            f"{ARTIFACT_CONTENT_BASE64_FIELD} must be a string",
        )
    try:
        content = base64.b64decode(raw, validate=True)
    except binascii.Error as error:
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_CONTENT_NOT_BASE64,
            f"{ARTIFACT_CONTENT_BASE64_FIELD} is not standard Base64 of the "
            f"exact bytes: {error}",
        ) from error
    if len(content) > MAXIMUM_MCP_ARTIFACT_BYTES:
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_PAYLOAD_TOO_LARGE,
            f"{len(content)} decoded bytes exceeds {MAXIMUM_MCP_ARTIFACT_BYTES}",
        )
    return content


def _artifact_file_bytes(raw: object) -> bytes:
    """The exact bytes of one local file, refused by name before any request.

    This child runs on the machine that named the path and with the trust the
    browser already has there, so reading the file is the same act as the
    caller reading it -- what it will not do is guess: the path is absolute,
    the file is regular, and it fits the store's bound, or nothing is sent.
    """

    if not isinstance(raw, str) or not raw:
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_PATH_NOT_ABSOLUTE,
            f"{ARTIFACT_PATH_FIELD} must be a nonempty absolute path",
        )
    try:
        named = Path(raw)
        if not named.is_absolute():
            raise McpLocalRefusal(McpRefusal.ARTIFACT_PATH_NOT_ABSOLUTE, raw)
        descriptor = os.open(named, os.O_RDONLY | os.O_NONBLOCK)
        try:
            described = os.fstat(descriptor)
            if not S_ISREG(described.st_mode):
                raise McpLocalRefusal(McpRefusal.ARTIFACT_PATH_NOT_A_REGULAR_FILE, raw)
            if described.st_size > MAXIMUM_ARTIFACT_BYTES:
                raise McpLocalRefusal(
                    McpRefusal.ARTIFACT_PATH_TOO_LARGE,
                    f"{described.st_size} bytes exceeds {MAXIMUM_ARTIFACT_BYTES}",
                )
            content = _bounded_artifact_file_read(descriptor)
        finally:
            os.close(descriptor)
    except FileNotFoundError as missing:
        raise McpLocalRefusal(McpRefusal.ARTIFACT_PATH_NOT_FOUND, raw) from missing
    except (OSError, ValueError) as unreadable:
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_PATH_UNREADABLE, f"{raw}: {unreadable}"
        ) from unreadable
    if len(content) > MAXIMUM_ARTIFACT_BYTES:
        raise McpLocalRefusal(
            McpRefusal.ARTIFACT_PATH_TOO_LARGE,
            f"{len(content)} bytes exceeds {MAXIMUM_ARTIFACT_BYTES}",
        )
    return content


def _bounded_artifact_file_read(descriptor: int) -> bytes:
    content = bytearray()
    maximum_read = MAXIMUM_ARTIFACT_BYTES + 1
    while len(content) < maximum_read:
        chunk = os.read(descriptor, maximum_read - len(content))
        if not chunk:
            break
        content.extend(chunk)
    return bytes(content)


def _required_text(arguments: Mapping[str, Any], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value:
        raise UnusableRunOrder(f"{field} must be a nonempty string")
    return value


def _require_loopback_service(service_url: str) -> None:
    address = urlsplit(service_url)
    if address.scheme not in ADDRESSABLE_SCHEMES or not address.netloc:
        raise McpServiceRefusal(
            f"{service_url!r} is not the address of a served Atelier API; "
            f"name one as {DEFAULT_SERVICE_URL!r}"
        )
    if not is_loopback_service_url(service_url):
        raise McpServiceRefusal(
            f"{service_url!r} is not a loopback Atelier API: this MCP child "
            "has no caller authentication, so it speaks only to the same "
            "machine the browser already trusts"
        )


def _post(
    api: AtelierApi, path: str, payload: bytes, *, media_type: str = JSON_MEDIA_TYPE
) -> bytes:
    try:
        return api.post(
            path, payload, media_type=media_type, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except AtelierApiTransportFailure as failure:
        raise _refused(failure) from failure


def _get(api: AtelierApi, path: str, *, accept: str = JSON_MEDIA_TYPE) -> bytes:
    try:
        return api.get(path, accept=accept, timeout=REQUEST_TIMEOUT_SECONDS)
    except AtelierApiTransportFailure as failure:
        raise _refused(failure) from failure


def _result(message_id: object, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}


def _error(message_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _tool_error(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": True,
    }
