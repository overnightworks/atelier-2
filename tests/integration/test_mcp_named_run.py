"""A stdio MCP client lists the catalog, starts a named run, and reads it terminal."""

from __future__ import annotations

import base64
import json
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

import pytest
import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    artifacts,
    context_packages_v3,
    node_execution_requests_v3,
    run_inputs_v3,
    runs,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX, CATALOG_LINEAGES_PATH
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import MAXIMUM_INSTANCE_DOCUMENT_BYTES
from atelier2.host.mcp_tools import McpToolName
from tests.host.test_mcp_server import StdioMcpSession
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import (
    api_limits,
    durable_ports,
    event_poll_backoff,
)
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

SERVED_PROJECT = ProjectId("atelier")
WORKFLOW_NAME = "mcp-named-line"
ARTIFACT_WORKFLOW_NAME = "mcp-artifact-line"
ORDER_NAME = "order"
PROVIDER_OUTPUT = b'"the exact provider bytes"'
DOCUMENT = (
    f"""format_version: 3
name: {WORKFLOW_NAME}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this named run is for.
""".encode()
    + declared_output()
)
ORDERED_DOCUMENT = (
    f"""format_version: 3
name: {ARTIFACT_WORKFLOW_NAME}
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: order-schema
      revision: {ANY_JSON_SCHEMA.revision_hash.value}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Review the ordered material.
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
""".encode()
    + declared_output()
)
STRICT_ORDER_WORKFLOW_NAME = "mcp-strict-order-line"
STRICT_ORDER_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    b'{"type": "object", "properties": {"portions": {"type": "integer", '
    b'"minimum": 1}}, "required": ["portions"], "additionalProperties": false}',
)
"""A schema a JSON-object diff order (`{"diff": "..."}`) never satisfies.

The strict-order line exists only to prove #438's refusal half: an artifact
whose bytes read as JSON but not as this shape is refused at start, by name,
before any run exists -- and the artifact that named it stays exactly where
`publish_artifact` left it.
"""
STRICT_ORDERED_DOCUMENT = (
    f"""format_version: 3
name: {STRICT_ORDER_WORKFLOW_NAME}
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: order-schema
      revision: {STRICT_ORDER_SCHEMA.revision_hash.value}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Review the ordered material.
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
""".encode()
    + declared_output()
)


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    yield from _started_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "mcp-named-run",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        tmp_path,
    )


@pytest.fixture
def served_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    """The same runtime, serving one project whose model defaults a start may read."""
    project_root = tmp_path / "project"
    git_project(project_root, declaring_verification(["true"]))
    yield from _started_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "mcp-named-run",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=SERVED_PROJECT,
            bootstrap_project_root=project_root,
        ),
        tmp_path,
    )


def _started_runtime(
    settings: DbosRuntimeSettings, tmp_path: Path
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        settings,
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (recording,),
    )
    started.initialize_storage()
    try:
        yield started, recording
    finally:
        started.close()


def application(runtime: DbosRuntime) -> FastAPI:
    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=durable_ports(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
        served_project_id=runtime.settings.project_id,
    )


@contextmanager
def live_server(app: FastAPI) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                log_level="critical",
                access_log=False,
                lifespan="off",
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=5)
            raise AssertionError("Uvicorn did not start for the MCP named-run proof")
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


@dataclass(frozen=True)
class PublishedLine:
    """What a start needs of a published named line: its lineage, one binding."""

    lineage_id: str
    configuration_hash: str


def publish_named_line(app: FastAPI, document: bytes = DOCUMENT) -> PublishedLine:
    """Publish the schema, the named document, its lineage and one binding."""

    api = TestClient(app)
    schema = api.post(
        API_PREFIX + "/schema-revisions",
        content=ANY_JSON_SCHEMA.document,
        headers={"content-type": "application/json"},
    )
    assert schema.status_code == 201, schema.text
    workflow = api.post(
        API_PREFIX + "/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )
    assert workflow.status_code == 201, workflow.text
    revision_hash = workflow.json()["workflow_revision_hash"]
    named = api.post(
        CATALOG_LINEAGES_PATH,
        json={
            "kind": RevisionKind.WORKFLOW.value,
            "catalog_revision_hash": revision_hash,
            "actor": "operator",
            "activated_at": "2026-08-17T00:00:00Z",
        },
    )
    assert named.status_code == 201, named.text
    auth = api.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 1,
            "provider_id": "exact",
            "auth_mode": "subscription",
        },
    )
    assert auth.status_code == 201, auth.text
    configuration = api.post(
        API_PREFIX + "/agent-configuration-revisions",
        json={
            "model": "opus",
            "auth_profile_revision_hash": auth.json()["auth_profile_revision_hash"],
            "executor_revision": "exact/v1",
        },
    )
    assert configuration.status_code == 201, configuration.text
    registry = api.put(
        f"{API_PREFIX}/model-registries/exact",
        json={
            "revision_number": 1,
            "entries": [
                {
                    "model_id": "opus",
                    "agent_configuration_revision_hash": configuration.json()[
                        "agent_configuration_revision_hash"
                    ],
                }
            ],
        },
    )
    assert registry.status_code in (200, 201), registry.text
    return PublishedLine(
        str(named.json()["lineage_id"]),
        str(configuration.json()["agent_configuration_revision_hash"]),
    )


def configure_project_model_default(app: FastAPI, line: PublishedLine) -> None:
    """Choose the published exact model as the served project's level-2 default."""
    project = encode_public_project_reference(SERVED_PROJECT)
    api = TestClient(app)
    registry = api.put(
        f"{API_PREFIX}/model-registries/exact",
        json={
            "revision_number": 1,
            "entries": [
                {
                    "model_id": "opus",
                    "agent_configuration_revision_hash": line.configuration_hash,
                }
            ],
        },
    )
    assert registry.status_code in (200, 201), registry.text
    defaults = api.put(
        f"{API_PREFIX}/projects/{project}/model-defaults",
        json={
            "revision_number": 1,
            "defaults": [
                {
                    "difficulty": 2,
                    "model_registry_revision_hash": registry.json()[
                        "model_registry_revision_hash"
                    ],
                    "provider_id": "exact",
                    "model_id": "opus",
                    "agent_configuration_revision_hash": line.configuration_hash,
                }
            ],
        },
    )
    assert defaults.status_code == 201, defaults.text


def wait_for_terminal(client: StdioMcpSession, reference: str) -> dict[str, object]:
    deadline = time.monotonic() + 8
    observed: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload, is_error = client.call_tool(
            McpToolName.RUN_STATUS.value, {"public_run_reference": reference}
        )
        assert not is_error
        assert isinstance(payload, dict)
        observed = payload
        if payload.get("state") in {
            RunState.COMPLETED.value,
            RunState.FAILED.value,
        }:
            return payload
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed.get('state')!r}, expected terminal")


def publish_schema(app: FastAPI, revision: PublishedRevision) -> None:
    """Publish one schema revision a strict-order line's graph input pins."""
    response = TestClient(app).post(
        API_PREFIX + "/schema-revisions",
        content=revision.document,
        headers={"content-type": "application/json"},
    )
    assert response.status_code in (200, 201), response.text


def stored_run_input(runtime: DbosRuntime, run_id: str) -> tuple[bytes, str]:
    """The exact bytes and content hash this run's order was stored under."""
    with runtime.engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(run_inputs_v3.c.value, run_inputs_v3.c.value_hash).where(
                    run_inputs_v3.c.run_id == run_id
                )
            )
            .mappings()
            .one()
        )
    return bytes(row["value"]), str(row["value_hash"])


def stored_context_package_manifest(
    runtime: DbosRuntime, run_id: str, revision_hash: str, node_id: str
) -> bytes:
    """The manifest of one node's persisted context-package/v3, read back."""
    execution_id = NodeExecutionId.for_node(
        RunId(run_id), WorkflowRevisionHash(revision_hash), node_id
    )
    with runtime.engine.connect() as connection:
        package_hash = connection.scalar(
            sa.select(node_execution_requests_v3.c.context_package_hash).where(
                node_execution_requests_v3.c.node_execution_id == execution_id.value
            )
        )
        manifest = connection.scalar(
            sa.select(context_packages_v3.c.manifest).where(
                context_packages_v3.c.package_hash == package_hash
            )
        )
    assert package_hash is not None
    assert manifest is not None
    return bytes(manifest)


def artifact_row_count(runtime: DbosRuntime, artifact_hash: str) -> int:
    """How many artifact rows this address names -- one when publish is honest."""
    with runtime.engine.connect() as connection:
        count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(artifacts)
            .where(artifacts.c.artifact_hash == artifact_hash)
        )
    assert count is not None
    return count


def run_row_count(runtime: DbosRuntime, run_id: str) -> int:
    """Whether a refused start left a run row behind."""
    with runtime.engine.connect() as connection:
        count = connection.scalar(
            sa.select(sa.func.count()).select_from(runs).where(runs.c.run_id == run_id)
        )
    assert count is not None
    return count


@pytest.mark.proves("a-full-pull-request-diff-reaches-its-agent-as-an-artifact")
@pytest.mark.proves("a-run-input-binds-as-a-materialized-package-member")
def test_the_mcp_published_hash_is_the_stored_order_the_package_member_and_the_job_bytes(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """One ~100 KB diff, published through MCP: the same identity in four places.

    `publish_artifact` answers a hash; the started run's stored order carries
    that same hash under those same bytes; the node's persisted context-package
    names the same hash as its material member; and the job the agent executor
    actually opened contains the diff whole, not a hash standing in for it.
    #438's slice 2 (#1142), proven through the MCP door rather than the HTTP
    route or the CLI that `349-a-full-diff-reaches-its-agent.toml` and
    `295-the-record-family-gets-its-writer.toml` already cover.
    """
    started_runtime, recording = runtime
    app = application(started_runtime)
    line = publish_named_line(app, ORDERED_DOCUMENT)
    diff = json.dumps({"diff": "x" * 100_000}).encode()
    assert len(diff) > 6 * MAXIMUM_INSTANCE_DOCUMENT_BYTES
    run_id = "mcp/one-identity"

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            published, publish_failed = client.call_tool(
                McpToolName.PUBLISH_ARTIFACT.value,
                {"content_base64": base64.standard_b64encode(diff).decode("ascii")},
            )
            assert not publish_failed, published
            address = str(published["artifact_hash"])

            started, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {
                    "name": ARTIFACT_WORKFLOW_NAME,
                    "run_id": run_id,
                    "agent_bindings": [
                        {
                            "role": "builder",
                            "agent_configuration_revision_hash": (
                                line.configuration_hash
                            ),
                        }
                    ],
                    "orders": [{"name": ORDER_NAME, "artifact_hash": address}],
                },
            )
            assert not start_failed, started
            revision_hash = str(started["workflow_revision_hash"])

            started_runtime.launch()
            ended = wait_for_terminal(client, str(started["public_run_reference"]))
        finally:
            client.close()

    assert ended["state"] == RunState.COMPLETED.value

    stored_value, stored_hash = stored_run_input(started_runtime, run_id)
    assert stored_value == diff
    assert stored_hash == address

    manifest = stored_context_package_manifest(
        started_runtime, run_id, revision_hash, "implement"
    )
    assert address.encode("ascii") in manifest

    assert recording.opened is not None
    assert diff in recording.opened.requests[0].job_bytes


@pytest.mark.proves("publishing-the-same-bytes-twice-through-mcp-stays-one-artifact")
def test_republishing_the_same_bytes_through_mcp_answers_one_hash_and_one_artifact(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The same bytes, published twice through MCP: one hash, one stored row.

    #438 lines 5 and 10: a second publish of identical bytes is not a second
    identity and not an error -- it answers the address the first publish
    already minted.
    """
    started_runtime, _recording = runtime
    app = application(started_runtime)
    diff = json.dumps({"diff": "y" * 100_000}).encode()
    encoded = base64.standard_b64encode(diff).decode("ascii")

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            first, first_failed = client.call_tool(
                McpToolName.PUBLISH_ARTIFACT.value, {"content_base64": encoded}
            )
            assert not first_failed, first
            second, second_failed = client.call_tool(
                McpToolName.PUBLISH_ARTIFACT.value, {"content_base64": encoded}
            )
            assert not second_failed, second
        finally:
            client.close()

    address = str(first["artifact_hash"])
    assert second["artifact_hash"] == address
    assert artifact_row_count(started_runtime, address) == 1


@pytest.mark.proves(
    "a-schema-refused-start-leaves-its-named-artifact-usable-and-writes-no-run"
)
@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
def test_a_schema_refused_start_leaves_the_mcp_published_artifact_usable_and_writes_no_run(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A start the pinned schema refuses leaves its named artifact untouched.

    #438 line 9/11/12: an order whose bytes do not satisfy the schema the
    document pinned is refused by name before any run row exists, and the
    artifact that named it is unaffected -- still readable at its own address.
    """
    started_runtime, _recording = runtime
    app = application(started_runtime)
    publish_schema(app, STRICT_ORDER_SCHEMA)
    line = publish_named_line(app, STRICT_ORDERED_DOCUMENT)
    diff = json.dumps({"diff": "z" * 100_000}).encode()
    run_id = "mcp/refused-schema"

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            published, publish_failed = client.call_tool(
                McpToolName.PUBLISH_ARTIFACT.value,
                {"content_base64": base64.standard_b64encode(diff).decode("ascii")},
            )
            assert not publish_failed, published
            address = str(published["artifact_hash"])

            refused, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {
                    "name": STRICT_ORDER_WORKFLOW_NAME,
                    "run_id": run_id,
                    "agent_bindings": [
                        {
                            "role": "builder",
                            "agent_configuration_revision_hash": (
                                line.configuration_hash
                            ),
                        }
                    ],
                    "orders": [{"name": ORDER_NAME, "artifact_hash": address}],
                },
            )
        finally:
            client.close()

    assert start_failed, refused
    assert str(refused["type"]).endswith("run-input-refused")
    assert run_row_count(started_runtime, run_id) == 0

    reread = TestClient(app).get(f"{API_PREFIX}/artifacts/{address}")
    assert reread.status_code == 200
    assert reread.content == diff


@pytest.mark.proves("a-stdio-client-lists-starts-and-reads-a-named-run")
def test_a_stdio_client_lists_the_catalog_starts_by_name_and_reads_terminal(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    started_runtime, _recording = runtime
    app = application(started_runtime)
    configuration_hash = publish_named_line(app).configuration_hash

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            listed, list_failed = client.call_tool(McpToolName.LIST_WORKFLOWS.value, {})
            assert not list_failed
            assert isinstance(listed, dict)
            names = [item["display_name"] for item in listed["items"]]
            assert names == [WORKFLOW_NAME]
            assert listed["items"][0]["revision_number"] == 1

            started, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {
                    "name": WORKFLOW_NAME,
                    "run_id": "mcp/named-line",
                    "agent_bindings": [
                        {
                            "role": "builder",
                            "agent_configuration_revision_hash": configuration_hash,
                        }
                    ],
                },
            )
            assert not start_failed
            assert isinstance(started, dict)
            assert started["state"] == RunState.STARTED.value
            reference = str(started["public_run_reference"])

            started_runtime.launch()
            ended = wait_for_terminal(client, reference)
        finally:
            client.close()

    assert ended["state"] == RunState.COMPLETED.value
    assert ended["terminal_hash"] is not None
    assert (
        ended["workflow_revision_hash"] == listed["items"][0]["catalog_revision_hash"]
    )


@pytest.mark.proves("a-stdio-client-lists-starts-and-reads-a-named-run")
def test_a_stdio_client_publishes_an_artifact_starts_by_address_and_reads_terminal(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    started_runtime, recording = runtime
    app = application(started_runtime)
    configuration_hash = publish_named_line(app, ORDERED_DOCUMENT).configuration_hash
    ordered_material = json.dumps({"diff": "x" * 20_000}).encode()
    assert len(ordered_material) > MAXIMUM_INSTANCE_DOCUMENT_BYTES

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            published, publish_failed = client.call_tool(
                McpToolName.PUBLISH_ARTIFACT.value,
                {
                    "content_base64": base64.standard_b64encode(
                        ordered_material
                    ).decode("ascii")
                },
            )
            assert not publish_failed
            assert isinstance(published, dict)
            address = str(published["artifact_hash"])

            started, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {
                    "name": ARTIFACT_WORKFLOW_NAME,
                    "run_id": "mcp/artifact-order",
                    "agent_bindings": [
                        {
                            "role": "builder",
                            "agent_configuration_revision_hash": configuration_hash,
                        }
                    ],
                    "orders": [{"name": ORDER_NAME, "artifact_hash": address}],
                },
            )
            assert not start_failed
            assert isinstance(started, dict)
            assert started["state"] == RunState.STARTED.value
            reference = str(started["public_run_reference"])

            started_runtime.launch()
            ended = wait_for_terminal(client, reference)
        finally:
            client.close()

    assert ended["state"] == RunState.COMPLETED.value
    assert ended["terminal_hash"] is not None
    assert recording.opened is not None
    assert ordered_material in recording.opened.requests[0].job_bytes


@pytest.mark.proves("mcp-and-http-never-diverge")
def test_a_stdio_start_without_bindings_runs_on_the_projects_model_default(
    served_runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The conductor names no binding; the served project's level default does.

    Both doors are one start: the MCP tool posts the body the HTTP door takes,
    and the starter resolves roles nobody bound from project configuration. A
    revision the described listing calls executable therefore starts from the
    stdio door exactly as it does from the console (#701).
    """
    started_runtime, _recording = served_runtime
    app = application(started_runtime)
    line = publish_named_line(app)
    configure_project_model_default(app, line)

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            listed, list_failed = client.call_tool(McpToolName.LIST_WORKFLOWS.value, {})
            assert not list_failed
            assert isinstance(listed, dict)
            assert [item["display_name"] for item in listed["items"]] == [WORKFLOW_NAME]

            started, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {"name": WORKFLOW_NAME, "run_id": "mcp/occupied-line"},
            )
            assert not start_failed, started
            assert isinstance(started, dict)
            assert started["state"] == RunState.STARTED.value
            assert [
                (binding["role"], binding["agent_configuration_revision_hash"])
                for binding in started["agent_bindings"]
            ] == [("builder", line.configuration_hash)]

            started_runtime.launch()
            ended = wait_for_terminal(client, str(started["public_run_reference"]))
        finally:
            client.close()

    assert ended["state"] == RunState.COMPLETED.value
