"""A subscriber who does not already know a run holds GET /events.

Opening that stream is the subscription. The proof is live delivery of a
V3 WAITING_INPUT and exclusive resume from the event1 cursor the feed itself
emitted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from threading import Event, Thread
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import event as engine_events
from sqlalchemy.exc import DatabaseError

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.attention_events import (
    ATTENTION_PAGE_UNREADABLE_EVENT,
    ATTENTION_ROW_UNREADABLE_EVENT,
)
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.instants import record_event_instant
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    run_agent_bindings,
    run_configuration_revisions,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.yaml_workflows import (
    WorkflowFormatNotExecutable,
    parse_executable_workflow_document,
)
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptState,
    RunnerEvidenceAcceptancePhase,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventKind,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.when import RecordedAt
from atelier2.host.logging import PROCESS_LOGGER_NAME
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


class SubscriptionBroke(Exception):
    """The subscriber never received what it held the feed for."""


def _sse_frames(
    response: httpx.Response, deadline: float
) -> Iterator[dict[str, object]]:
    """Every frame the feed sends until it ends; a failure frame carries no id."""
    fields: dict[str, str] = {}
    for line in response.iter_lines():
        if time.monotonic() > deadline:
            raise SubscriptionBroke("timed out waiting for an attention event")
        if line == "":
            if fields:
                payload: dict[str, object] = json.loads(fields["data"])
                yield {"id": fields.get("id"), "data": payload}
                fields = {}
            continue
        name, value = line.split(": ", maxsplit=1)
        fields[name] = value


def _frame_event(frame: dict[str, object]) -> object:
    data = frame["data"]
    assert isinstance(data, dict)
    return data["event"]


def _hold_until(
    url: str,
    connected: Event,
    received: list[dict[str, object]],
    errors: list[BaseException],
    last: Callable[[dict[str, object]], bool],
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
            if response.status_code != HTTPStatus.OK:
                raise SubscriptionBroke(
                    f"the attention feed answered {response.status_code}: "
                    f"{response.read()!r}"
                )
            connected.set()
            for frame in _sse_frames(response, time.monotonic() + 12):
                received.append(frame)
                if last(frame):
                    return
    except (
        SubscriptionBroke,
        httpx.HTTPError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        errors.append(error)
        connected.set()


def _feed_until(
    port: int,
    start: Callable[[], None],
    last: Callable[[dict[str, object]], bool],
    last_event_id: str | None = None,
) -> list[dict[str, object]]:
    """The frames a subscriber receives, up to the one `last` accepts."""
    connected = Event()
    received: list[dict[str, object]] = []
    errors: list[BaseException] = []
    headers = {"Last-Event-ID": last_event_id} if last_event_id is not None else None
    thread = Thread(
        target=_hold_until,
        args=(
            f"http://127.0.0.1:{port}{ATTENTION_EVENTS_PATH}",
            connected,
            received,
            errors,
            last,
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
    return received


def _waiting_input_from(
    port: int,
    start: Callable[[], None],
    last_event_id: str | None = None,
) -> dict[str, object]:
    first = _feed_until(port, start, lambda _frame: True, last_event_id)[0]
    assert first["id"] is not None
    return first


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


REFUSED_DOCUMENT = b"""format_version: 3
name: Stored before today's parser refused it
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
UNREADABLE_RUN = RunId("v3/failed-on-a-revision-today-refuses")
STORED_AT = RecordedAt("2026-08-31T19:40:30Z")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _seed_failure_on_a_refused_revision(
    runtime: DbosRuntime, configuration: AgentConfigurationRevision
) -> None:
    """One FAILED run whose AGENT_FAILED event today's parser cannot project.

    Projecting that failure reads its attempt's refusal receipt, which loads the
    attempt's stored revision through the executable parser -- and that parser
    refuses the revision the run was started on.
    """
    with pytest.raises(WorkflowFormatNotExecutable):
        parse_executable_workflow_document(REFUSED_DOCUMENT)
    revision = WorkflowRevision(REFUSED_DOCUMENT)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    execution_id = NodeExecutionId.for_node(
        UNREADABLE_RUN, revision.revision_hash, "implement"
    )
    request_hash = AgentExecutionRequestHash(_sha256("unreadable run request"))
    attempt_id = AgentAttemptId.for_execution(execution_id, request_hash)
    failure_code = AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
    failed = RunEvent(
        UNREADABLE_RUN,
        revision.revision_hash,
        1,
        "implement",
        execution_id,
        RunEventKind.AGENT_FAILED,
        failure_code.value.encode("ascii"),
        attempt_binding=RunEventAgentAttemptBinding(attempt_id, 1),
    )
    configuration_hash = _sha256("unreadable run configuration")
    with runtime.engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert(),
            {
                "revision_hash": revision.revision_hash.value,
                "document": revision.document,
            },
        )
        connection.execute(
            run_configuration_revisions.insert(),
            {"revision_hash": configuration_hash, "preimage": b"unreadable run"},
        )
        connection.execute(
            runs.insert(),
            {
                "run_id": UNREADABLE_RUN.value,
                "bootstrap_workflow_id": f"workflow-{UNREADABLE_RUN.value}",
                "revision_hash": revision.revision_hash.value,
                "workflow_format_version": 3,
                "agent_binding_set_hash": bindings.binding_set_hash.value,
                "run_configuration_revision_hash": configuration_hash,
                "current_node_id": "implement",
                "current_round_ordinal": FIRST_ROUND_ORDINAL,
                "state": RunState.FAILED.value,
                "state_version": 1,
                "last_event_sequence": 1,
                "terminal_hash": _sha256("unreadable run end"),
            },
        )
        connection.execute(
            run_agent_bindings.insert(),
            {
                "run_id": UNREADABLE_RUN.value,
                "revision_hash": revision.revision_hash.value,
                "binding_set_hash": bindings.binding_set_hash.value,
                "role": "builder",
                "agent_configuration_revision_hash": configuration.revision_hash.value,
            },
        )
        connection.execute(
            agent_attempts.insert(),
            {
                "attempt_id": attempt_id.value,
                "node_execution_id": execution_id.value,
                "request_hash": request_hash.value,
                "executor_operational_identity": "exact-operation",
                "run_id": UNREADABLE_RUN.value,
                "workflow_revision_hash": revision.revision_hash.value,
                "node_id": "implement",
                "attempt_ordinal": 1,
                "state": AgentAttemptState.FAILED.value,
                "state_version": 2,
                "process_phase": AgentAttemptProcessPhase.NONE.value,
                "failure_code": failure_code.value,
                "runner_evidence_acceptance_phase": (
                    RunnerEvidenceAcceptancePhase.NONE.value
                ),
            },
        )
        connection.execute(
            run_events.insert(),
            {
                "run_id": failed.run_id.value,
                "revision_hash": failed.revision_hash.value,
                "event_sequence": failed.event_sequence,
                "node_id": failed.node_id,
                "node_execution_id": failed.node_execution_id.value,
                "round_ordinal": failed.round_ordinal,
                "event_kind": failed.event_kind.value,
                "payload": failed.payload,
                "payload_hash": failed.payload_hash.value,
                "event_hash": failed.event_hash.value,
                "agent_attempt_id": attempt_id.value,
                "attempt_ordinal": 1,
            },
        )
        record_event_instant(connection, UNREADABLE_RUN.value, 1, at=STORED_AT)


def _journal(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if getattr(record, "event", None) == event
    ]


def test_a_run_today_cannot_project_is_named_and_the_feed_delivers_the_others(
    runtime: tuple[DbosRuntime, AgentConfigurationRevision, WorkflowRevision],
    caplog: pytest.LogCaptureFixture,
) -> None:
    started, configuration, workflow = runtime
    _seed_failure_on_a_refused_revision(started, configuration)
    healthy_run = RunId("v3/attention-wait-beside-the-unreadable")

    with (
        caplog.at_level(logging.ERROR, logger=PROCESS_LOGGER_NAME),
        live_attention_server(started) as port,
    ):
        frames = _feed_until(
            port,
            lambda: start_wait_run(started, healthy_run, workflow, configuration),
            lambda frame: _frame_event(frame) in {"WAITING_INPUT", "STREAM_FAILED"},
        )

    assert [_frame_event(frame) for frame in frames] == [
        "RUN_PROJECTION_CORRUPT",
        "WAITING_INPUT",
    ]
    named, delivered = (frame["data"] for frame in frames)
    assert isinstance(named, dict)
    assert isinstance(delivered, dict)
    assert named["public_run_reference"] == encode_public_run_reference(UNREADABLE_RUN)
    assert delivered["public_run_reference"] == encode_public_run_reference(healthy_run)
    journal = _journal(caplog, ATTENTION_ROW_UNREADABLE_EVENT)
    assert {getattr(record, "run_id", None) for record in journal} == {
        UNREADABLE_RUN.value
    }
    # The class, never the parser's text: that text can quote the stored document.
    assert all(record.exc_info is None for record in journal)
    assert all(
        record.getMessage().endswith("event_sequence=1: WorkflowFormatNotExecutable")
        for record in journal
    )


def _malformed_store(statement: str) -> DatabaseError:
    return DatabaseError(
        statement, None, sqlite3.DatabaseError("database disk image is malformed")
    )


def _refuse_attempt_reads(
    _connection: Any,
    _cursor: Any,
    statement: str,
    _parameters: Any,
    _context: Any,
    _executemany: bool,
) -> None:
    if "FROM agent_attempts" in statement:
        raise _malformed_store(statement)


def _refuse_every_connection(_connection: Any) -> None:
    raise _malformed_store("connect")


STORE_ERRORS = (
    pytest.param(
        "before_cursor_execute", _refuse_attempt_reads, id="while-projecting-a-row"
    ),
    pytest.param(
        "engine_connect", _refuse_every_connection, id="while-opening-the-connection"
    ),
)
"""Where a store error meets the feed: inside one run's projection, or before
any row, while the page's connection opens."""


@pytest.mark.parametrize(("hook", "refuse"), STORE_ERRORS)
def test_a_store_error_ends_the_feed_loudly_and_blames_no_run(
    runtime: tuple[DbosRuntime, AgentConfigurationRevision, WorkflowRevision],
    caplog: pytest.LogCaptureFixture,
    hook: str,
    refuse: Callable[..., None],
) -> None:
    """A store error proves nothing about one run, so no run is blamed for it."""
    started, configuration, _workflow = runtime
    _seed_failure_on_a_refused_revision(started, configuration)

    with (
        caplog.at_level(logging.ERROR, logger=PROCESS_LOGGER_NAME),
        live_attention_server(started) as port,
    ):
        engine_events.listen(started.engine, hook, refuse)
        try:
            frames = _feed_until(port, lambda: None, lambda _frame: True)
        finally:
            engine_events.remove(started.engine, hook, refuse)

    assert [_frame_event(frame) for frame in frames] == ["STREAM_FAILED"]
    failure = frames[0]["data"]
    assert isinstance(failure, dict)
    assert str(failure["problem"]["type"]).endswith("durable-state-corrupt")
    journal = _journal(caplog, ATTENTION_PAGE_UNREADABLE_EVENT)
    assert journal
    assert all(record.exc_info is None for record in journal)
    assert all(
        record.getMessage().endswith(": DatabaseError")
        and "agent_attempts" not in record.getMessage()
        and "malformed" not in record.getMessage()
        for record in journal
    )
