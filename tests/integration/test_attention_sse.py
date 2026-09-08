"""A subscriber who does not already know a run holds GET /events.

Opening that stream is the subscription. The proof is live delivery of a
V3 WAITING_INPUT and exclusive resume from the event1 cursor the feed itself
emitted.
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Any

import httpx
import pytest
import uvicorn

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_asgi_app, event_poll_backoff
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

APPROVAL_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "string"}')
PROVIDER_OUTPUT = b'"the exact provider bytes"'
WAIT_DOCUMENT = (
    b"""format_version: 3
name: A person approves last
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
"""
    + declared_output(APPROVAL_SCHEMA, "approval")
)
ATTENTION_EVENTS_PATH = "/atelier/api/v1/events"


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, AgentConfigurationRevision, WorkflowRevision]]:
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "attention-sse", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (recording,),
    )
    started.initialize_storage()
    catalog_store = DbosCatalogStore(started.engine)
    for schema in (ANY_JSON_SCHEMA, APPROVAL_SCHEMA):
        assert isinstance(
            catalog_store.publish_revision(schema),
            (PublishedRevisionCreated, PublishedRevisionExisting),
        )
    catalog = DbosAgentConfigurationCatalog(
        started.engine, started.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        started.engine, ProviderId("exact"), (configuration,)
    )
    workflow = WorkflowRevision(WAIT_DOCUMENT)
    DbosWorkflowRevisionPublisher(started.engine).publish(workflow)
    try:
        yield started, configuration, workflow
    finally:
        started.close()


def start_wait_run(
    runtime: DbosRuntime,
    run_id: RunId,
    workflow: WorkflowRevision,
    configuration: AgentConfigurationRevision,
) -> None:
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            run_id,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
            ),
        )
    )
    assert isinstance(started, DurableRunCreated), started
    runtime.launch()


@contextmanager
def live_attention_server(runtime: DbosRuntime) -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                durable_asgi_app(runtime, poll_backoff=event_poll_backoff()),
                host="127.0.0.1",
                port=0,
                log_level="critical",
                access_log=False,
                lifespan="off",
            )
        )
        thread = Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=5)
            raise AssertionError("Uvicorn did not start for the attention feed")
        try:
            yield port
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def _read_one_sse_event(response: httpx.Response, deadline: float) -> dict[str, object]:
    fields: dict[str, str] = {}
    for line in response.iter_lines():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for an attention event")
        if line == "":
            if not fields:
                continue
            assert "id" in fields
            assert "data" in fields
            payload: dict[str, object] = json.loads(fields["data"])
            return {"id": fields["id"], "data": payload}
        name, value = line.split(": ", maxsplit=1)
        fields[name] = value
    raise AssertionError("stream ended before an attention event")


def _hold_until_event(
    url: str,
    connected: Event,
    received: list[dict[str, object]],
    errors: list[BaseException],
    headers: dict[str, str] | None = None,
) -> None:
    request_headers = {"Accept": "text/event-stream"}
    if headers:
        request_headers.update(headers)
    try:
        with httpx.stream(
            "GET",
            url,
            headers=request_headers,
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
        ) as response:
            assert response.status_code == 200, response.read()
            connected.set()
            received.append(_read_one_sse_event(response, time.monotonic() + 12))
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
        errors.append(error)
        connected.set()


def _waiting_input_from(
    port: int,
    start: Any,
    last_event_id: str | None = None,
) -> dict[str, object]:
    connected = Event()
    received: list[dict[str, object]] = []
    errors: list[BaseException] = []
    headers = {"Last-Event-ID": last_event_id} if last_event_id is not None else None
    thread = Thread(
        target=_hold_until_event,
        args=(
            f"http://127.0.0.1:{port}{ATTENTION_EVENTS_PATH}",
            connected,
            received,
            errors,
            headers,
        ),
        daemon=True,
    )
    thread.start()
    assert connected.wait(5), errors
    start()
    thread.join(timeout=15)
    if errors:
        raise errors[0]
    assert received, "the attention feed emitted nothing"
    return received[0]


@pytest.mark.proves("a-client-holding-events-receives-a-wait")
def test_a_client_holding_events_receives_waiting_input_and_resume_does_not_duplicate(
    runtime: tuple[DbosRuntime, AgentConfigurationRevision, WorkflowRevision],
) -> None:
    started, configuration, workflow = runtime
    first_run = RunId("v3/attention-wait-one")
    second_run = RunId("v3/attention-wait-two")
    with live_attention_server(started) as port:
        first = _waiting_input_from(
            port,
            lambda: start_wait_run(started, first_run, workflow, configuration),
        )
        first_data = first["data"]
        assert isinstance(first_data, dict)
        assert first["id"] == first_data["cursor"]
        assert first_data["event"] == "WAITING_INPUT"
        assert first_data["workflow_format_version"] == 3
        assert first_data["node_id"] == "approve"

        second = _waiting_input_from(
            port,
            lambda: start_wait_run(started, second_run, workflow, configuration),
            last_event_id=str(first["id"]),
        )
        second_data = second["data"]
        assert isinstance(second_data, dict)
        assert second["id"] != first["id"]
        assert second_data["event"] == "WAITING_INPUT"
        assert second_data["cursor"] != first_data["cursor"]
        assert second["id"] == second_data["cursor"]
