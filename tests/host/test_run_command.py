"""What `atelier2 run` does against a service that answers the published API.

The service here is a real HTTP server speaking the product's own resources, so
these tests pin the command's conversation and its operator-visible answer, not
the shape of an internal call.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Literal, Self

import pytest
from pydantic import ValidationError

from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import problem_resource
from atelier2.api.references import encode_canonical_base64
from atelier2.api.wire.events import (
    AgentCompletedEventResourceV3,
    AgentFailedEventResourceV3,
    WaitingInputEventResourceV3,
)
from atelier2.api.wire.requests import PublishAgentConfigurationRevisionRequestResource
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionResource,
    ArtifactResource,
    AuthProfileRevisionResource,
    CatalogAdmissionResource,
    NodeDetailResource,
    NodeRailResource,
    NodeStateName,
    ProblemResource,
    RunCancellabilityResource,
    RunResourceV3,
    StreamFailureResource,
    WorkflowGraphResourceV3,
    WorkflowNodePreviewResourceV3,
    WorkflowRevisionDetailResource,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.host import main
from atelier2.host.run_command import (
    AGENT_CONFIGURATION_PATH,
    ARTIFACT_PATH,
    AUTH_PROFILE_PATH,
    CATALOG_LINEAGE_PATH,
    COMMAND_CATALOG_ACTOR,
    JSON_MEDIA_TYPE,
    OCTET_STREAM_MEDIA_TYPE,
    RUN_PATH,
    WORKFLOW_REVISION_PATH,
    AgentRoleBinding,
    NameOrder,
    ServiceRefused,
    catalog_name_path,
    derived_run_id,
    describe_resolution,
    resolve_published_name,
)

PROBLEM_MEDIA_TYPE = "application/problem+json"
EVENT_STREAM_MEDIA_TYPE = "text/event-stream"

AUTH_PROFILE_HASH = "a" * 64
AGENT_CONFIGURATION_HASH = "b" * 64
REVISION_HASH = "c" * 64
TERMINAL_HASH = "d" * 64
NODE_EXECUTION_ID = "f" * 64
EVENT_HASH = "1" * 64
ATTEMPT_ID = "2" * 64
BINDING_SET_HASH = "3" * 64
RUN_CONFIGURATION_HASH = "4" * 64
ARTIFACT_HASH = "9" * 64
SECOND_ARTIFACT_HASH = "8" * 64

PUBLIC_RUN_REFERENCE = "run1.dGVzdA"
EVENT_CURSOR = f"event1.dGVzdA.{1}"
LATER_EVENT_CURSOR = f"event1.dGVzdA.{2}"
STREAM_FAILURE_CODE = "durable-state-corrupt"
AGENT_ROLE = "writer"
AGENT_NODE_ID = "draft"
TERMINAL_NODE_ID = "total"
AGENT_OUTPUT = b"the answer the run produced"
OUTPUT_HASH = Sha256Hash.of(AGENT_OUTPUT).value
"""The hash the service stamps on that output, minted by its own owner."""

WORKFLOW_NAME = "diff-review"
WORKFLOW_DOCUMENT = b"""format_version: 3
name: diff-review
description: Review a bound diff.
nodes:
  - id: draft
    type: agent
    role: writer
    mode: headless
    instruction: Review the bound diff.
"""
BINDING_DOCUMENT = json.dumps(
    {
        "auth_profile": {
            "profile_id": "personal",
            "revision_number": 1,
            "provider_id": "claude",
            "auth_mode": "subscription",
        },
        "model": "claude-opus-4",
        "executor_revision": "claude-subscription-v1",
    }
).encode()
WIRE_CAPABILITY_DEFAULT = PublishAgentConfigurationRevisionRequestResource.model_fields[
    "requested_capability"
].default


def binding_document(**fields: object) -> bytes:
    payload = json.loads(BINDING_DOCUMENT)
    payload.update(fields)
    return json.dumps(payload).encode()


RUNS_URL_PATH = API_PREFIX + RUN_PATH
RUN_URL_PATH = f"{RUNS_URL_PATH}/{PUBLIC_RUN_REFERENCE}"
EVENTS_URL_PATH = f"{RUN_URL_PATH}/events"
ARTIFACTS_URL_PATH = API_PREFIX + ARTIFACT_PATH
NODE_DETAIL_URL_PATH = f"{RUN_URL_PATH}/nodes/{AGENT_NODE_ID}"


@dataclass(frozen=True)
class Answer:
    body: bytes
    status: HTTPStatus = HTTPStatus.OK
    media_type: str = JSON_MEDIA_TYPE


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: bytes
    content_type: str = ""


@dataclass
class ScriptedService:
    """One real HTTP server answering the routes this command uses."""

    answers: dict[tuple[str, str], list[Answer]]
    calls: list[Call] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None

    def __enter__(self) -> Self:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._answer("GET")

            def do_POST(self) -> None:
                self._answer("POST")

            def _answer(self, method: str) -> None:
                length = int(self.headers.get("content-length", "0"))
                content_type = self.headers.get("content-type", "")
                service.calls.append(
                    Call(method, self.path, self.rfile.read(length), content_type)
                )
                scripted = service.answers.get((method, self.path))
                answer = (
                    unrouted_answer()
                    if not scripted
                    else (scripted.pop(0) if len(scripted) > 1 else scripted[0])
                )
                self.send_response(answer.status)
                self.send_header("content-type", answer.media_type)
                self.send_header("content-length", str(len(answer.body)))
                self.end_headers()
                self.wfile.write(answer.body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exception: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def sent(self, method: str, path: str) -> list[bytes]:
        return [
            call.body
            for call in self.calls
            if (call.method, call.path) == (method, path)
        ]


def unrouted_answer() -> Answer:
    return problem_answer(
        HTTPStatus.NOT_FOUND, "not-found", "Not Found", "no such resource"
    )


def problem_answer(status: HTTPStatus, kind: str, title: str, detail: str) -> Answer:
    problem = ProblemResource(type=kind, title=title, status=int(status), detail=detail)
    return Answer(
        problem.model_dump_json().encode(), status=status, media_type=PROBLEM_MEDIA_TYPE
    )


def published_auth_profile() -> Answer:
    return Answer(
        AuthProfileRevisionResource(
            profile_id="personal",
            revision_number=1,
            provider_id="claude",
            auth_mode="subscription",
            auth_profile_revision_hash=AUTH_PROFILE_HASH,
        )
        .model_dump_json()
        .encode()
    )


def published_agent_configuration(
    requested_capability: Literal["headless", "headless_with_tools", "interactive"] = (
        WIRE_CAPABILITY_DEFAULT
    ),
) -> Answer:
    return Answer(
        AgentConfigurationRevisionResource(
            model="claude-opus-4",
            auth_profile_revision_hash=AUTH_PROFILE_HASH,
            executor_revision="claude-subscription-v1",
            provider_id="claude",
            auth_mode="subscription",
            requested_capability=requested_capability,
            agent_configuration_revision_hash=AGENT_CONFIGURATION_HASH,
        )
        .model_dump_json()
        .encode()
    )


def published_artifact(artifact_hash: str = ARTIFACT_HASH) -> Answer:
    return Answer(
        ArtifactResource(artifact_hash=artifact_hash).model_dump_json().encode()
    )


def node_rail(terminal_state: NodeStateName) -> tuple[NodeRailResource, ...]:
    """The rail the service answers with, on the two nodes this command walks."""
    return (
        NodeRailResource(
            node_id=AGENT_NODE_ID, state=NodeState.SUCCEEDED, attempt=None
        ),
        NodeRailResource(node_id=TERMINAL_NODE_ID, state=terminal_state, attempt=None),
    )


def run_resource(
    state: Literal["STARTED", "COMPLETED"],
    terminal_hash: str | None,
    latest_event_cursor: str = EVENT_CURSOR,
    current_node_id: str = TERMINAL_NODE_ID,
) -> RunResourceV3:
    """The run the service answers with, which the command reads twice.

    The exit-0 contract is decided on what it reads back here, not only on the
    events, so the run and the events are the one shape the service speaks.
    """

    return RunResourceV3(
        workflow_format_version=3,
        run_id="unread-by-the-command",
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        workflow_name=WORKFLOW_NAME,
        agent_binding_set_hash=BINDING_SET_HASH,
        run_configuration_revision_hash=RUN_CONFIGURATION_HASH,
        agent_bindings=(),
        orders=(),
        state_version=2,
        state=state,
        current_node_id=current_node_id,
        current_node_execution_id=NODE_EXECUTION_ID,
        node_rail=node_rail(
            NodeState.SUCCEEDED if state == "COMPLETED" else NodeState.WORKING
        ),
        cancellation=RunCancellabilityResource(
            cancellable=False,
            reason="already-ended" if state == "COMPLETED" else "between-nodes",
            target_node_execution_id=None,
        ),
        terminal_hash=terminal_hash,
        latest_event_cursor=latest_event_cursor,
    )


def started_run() -> Answer:
    return Answer(run_resource("STARTED", None).model_dump_json().encode())


def completed_run(latest_event_cursor: str = EVENT_CURSOR) -> Answer:
    return Answer(
        run_resource("COMPLETED", TERMINAL_HASH, latest_event_cursor)
        .model_dump_json()
        .encode()
    )


def event_stream(*events: str, failure: str | None = None) -> Answer:
    """The frames exactly as the served API writes them: data, then id.

    A failure frame ends the stream and carries no id, because the route offers
    no resume cursor into its own refusal.
    """

    frames = "".join(f"data: {payload}\nid: {EVENT_CURSOR}\n\n" for payload in events)
    if failure is not None:
        frames += f"data: {failure}\n\n"
    return Answer(frames.encode(), media_type=EVENT_STREAM_MEDIA_TYPE)


def stream_failure(code: str = STREAM_FAILURE_CODE) -> str:
    """The frame the route writes when the stream itself fails, in its own words."""

    return StreamFailureResource(problem=problem_resource(code)).model_dump_json()


def agent_completed(
    node_id: str = AGENT_NODE_ID,
    output: bytes = AGENT_OUTPUT,
    sequence: int = 1,
    cursor: str = EVENT_CURSOR,
) -> str:
    """One agent completion: the output travels base64 beside its hash.

    The attempt names itself and the rail rides along, which is what lets the
    command report a run it started without deriving anything.
    """

    return AgentCompletedEventResourceV3(
        workflow_format_version=3,
        node_rail=node_rail(NodeState.WORKING),
        cursor=cursor,
        sequence=sequence,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id=node_id,
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="AGENT_COMPLETED",
        output_base64=encode_canonical_base64(output),
        output_hash=Sha256Hash.of(output).value,
        attempt_id=ATTEMPT_ID,
        attempt_ordinal=1,
    ).model_dump_json()


def agent_failed() -> str:
    return AgentFailedEventResourceV3(
        workflow_format_version=3,
        node_rail=node_rail(NodeState.WORKING),
        cursor=EVENT_CURSOR,
        sequence=1,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id=AGENT_NODE_ID,
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="AGENT_FAILED",
        failure_code=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        reason=None,
        attempt_id=ATTEMPT_ID,
        attempt_ordinal=1,
    ).model_dump_json()


def node_detail(refusal: str | None) -> Answer:
    """The node resource the command asks why a run stopped where it did.

    `refusal` absent is its own answer: a node whose ending nobody recorded is
    not a node that failed for no reason, and the command says which it read.
    """

    return Answer(
        NodeDetailResource(
            run_id="unread-by-the-command",
            public_run_reference=PUBLIC_RUN_REFERENCE,
            node_id=AGENT_NODE_ID,
            state=NodeState.FAILED,
            job_base64=None,
            job_hash=None,
            answer=None,
            provenance=None,
            refusal=refusal,
        )
        .model_dump_json()
        .encode()
    )


PROCESS_DIED_REASON = (
    "process-exited-unsuccessfully: exited with code 1; "
    "standard error: grok: prompt file rejected"
)


def waiting_for_input() -> str:
    return WaitingInputEventResourceV3(
        workflow_format_version=3,
        node_rail=node_rail(NodeState.WORKING),
        cursor=EVENT_CURSOR,
        sequence=1,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id="approval",
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="WAITING_INPUT",
    ).model_dump_json()


def serving_answers(
    **replacements: Answer,
) -> dict[tuple[str, str], list[Answer]]:
    """The whole conversation of one run that ends, with named replacements."""

    scripted = {
        "auth_profile": (
            "POST",
            API_PREFIX + AUTH_PROFILE_PATH,
            published_auth_profile(),
        ),
        "agent_configuration": (
            "POST",
            API_PREFIX + AGENT_CONFIGURATION_PATH,
            published_agent_configuration(),
        ),
        "workflow_revision": (
            "POST",
            API_PREFIX + WORKFLOW_REVISION_PATH,
            published_workflow_revision(),
        ),
        "artifact": ("POST", ARTIFACTS_URL_PATH, published_artifact()),
        "start": ("POST", RUNS_URL_PATH, started_run()),
        "events": ("GET", EVENTS_URL_PATH, event_stream(agent_completed())),
        "run": ("GET", RUN_URL_PATH, completed_run()),
        "node_detail": (
            "GET",
            NODE_DETAIL_URL_PATH,
            node_detail(PROCESS_DIED_REASON),
        ),
    }
    answers = {
        (method, path): [replacements.get(name, answer)]
        for name, (method, path, answer) in scripted.items()
    }
    answers[("POST", LINEAGES_URL_PATH)] = [founded_lineage()]
    return answers


CHAIN_SECOND_NODE_ID = "review"
CHAIN_SECOND_OUTPUT = b'"what the reviewer wrote"'


def chained_serving_answers() -> dict[tuple[str, str], list[Answer]]:
    """The conversation a chain has: two nodes, each handing its work on."""

    return serving_answers(
        start=Answer(
            run_resource("STARTED", None, current_node_id=CHAIN_SECOND_NODE_ID)
            .model_dump_json()
            .encode()
        ),
        events=event_stream(
            agent_completed(AGENT_NODE_ID, AGENT_OUTPUT, 1, EVENT_CURSOR),
            agent_completed(
                CHAIN_SECOND_NODE_ID, CHAIN_SECOND_OUTPUT, 2, LATER_EVENT_CURSOR
            ),
        ),
        run=Answer(
            run_resource(
                "COMPLETED",
                TERMINAL_HASH,
                LATER_EVENT_CURSOR,
                current_node_id=CHAIN_SECOND_NODE_ID,
            )
            .model_dump_json()
            .encode()
        ),
    )


def unbound_serving_answers() -> dict[tuple[str, str], list[Answer]]:
    """The same conversation for a workflow that binds no agent.

    The run reads back in one shape whether or not the caller named bindings --
    only the start request differs -- so this is the scripted conversation with
    nothing replaced, and `unbound_order` is what makes it the unbound one.
    """

    return serving_answers()


@pytest.fixture
def order(tmp_path: Path) -> Iterator[list[str]]:
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)
    binding = tmp_path / "writer.json"
    binding.write_bytes(BINDING_DOCUMENT)
    yield ["run", "--workflow", str(workflow), "--binding", f"{AGENT_ROLE}={binding}"]


@pytest.fixture
def unbound_order(tmp_path: Path) -> Iterator[list[str]]:
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)
    yield ["run", "--workflow", str(workflow)]


def run_command(order: list[str], service: ScriptedService, *extra: str) -> int:
    return main([*order, "--service", service.url, *extra])


def test_the_output_of_a_run_that_ended_is_printed_with_what_binds_it_to_that_run(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)
    reported = printed.err.decode()
    assert PUBLIC_RUN_REFERENCE in reported
    assert TERMINAL_HASH in reported
    assert OUTPUT_HASH in reported
    assert ATTEMPT_ID in reported


def test_an_event_kind_this_command_knows_nothing_about_is_passed_over(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """A history may carry kinds this client predates; only ours decide."""

    unknown = json.dumps({"event": "SOMETHING_LATER", "sequence": 1})
    with ScriptedService(
        serving_answers(events=event_stream(unknown, agent_completed()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)


def test_the_started_run_binds_the_hashes_the_service_answered_with(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        run_command(order, service)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])

    assert started == {
        "workflow_format_version": 2,
        "run_id": derived_run_id(
            REVISION_HASH, (AgentRoleBinding(AGENT_ROLE, AGENT_CONFIGURATION_HASH),)
        ),
        "workflow_revision_hash": REVISION_HASH,
        "agent_bindings": [
            {
                "role": AGENT_ROLE,
                "agent_configuration_revision_hash": AGENT_CONFIGURATION_HASH,
            }
        ],
    }


def test_a_workflow_that_binds_no_agent_starts_without_publishing_one(
    unbound_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(unbound_serving_answers()) as service:
        exit_code = run_command(unbound_order, service)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])
        published_agents = service.sent("POST", API_PREFIX + AUTH_PROFILE_PATH)

    assert (exit_code, published_agents) == (0, [])
    assert started == {
        "run_id": derived_run_id(REVISION_HASH, ()),
        "workflow_revision_hash": REVISION_HASH,
    }


def test_the_output_of_a_workflow_that_binds_no_agent_is_printed_as_it_was_written(
    unbound_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(unbound_serving_answers()) as service:
        exit_code = run_command(unbound_order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)
    reported = printed.err.decode()
    assert OUTPUT_HASH in reported
    # The host casts the roles the caller left unnamed, so the work still runs
    # under an attempt and the report still names it.
    assert ATTEMPT_ID in reported


def test_the_same_command_twice_asks_for_the_same_run(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        first = run_command(order, service)
        second = run_command(order, service)
        started = service.sent("POST", RUNS_URL_PATH)

    printed = capsysbinary.readouterr()
    assert (first, second) == (0, 0)
    assert printed.out == AGENT_OUTPUT + AGENT_OUTPUT
    assert started[0] == started[1]


def test_a_named_run_identity_is_the_one_asked_for(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        run_command(order, service, "--run-id", "the-operators-own-identity")
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])

    assert started["run_id"] == "the-operators-own-identity"


def test_a_failed_agent_attempt_ends_the_command_unsuccessfully(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(
        serving_answers(events=event_stream(agent_failed()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert b"PROCESS_EXITED_UNSUCCESSFULLY" in printed.err
    assert ATTEMPT_ID.encode() in printed.err


@pytest.mark.proves("a-dead-process-ends-its-attempt-durably-named")
@pytest.mark.parametrize(
    ("stored", "named"),
    (
        (PROCESS_DIED_REASON, PROCESS_DIED_REASON),
        (None, "no reason was recorded"),
    ),
    ids=("a reason the node kept", "a failure nothing recorded"),
)
def test_a_failed_attempt_is_reported_with_the_reason_its_node_kept(
    order: list[str],
    capsysbinary: pytest.CaptureFixture[bytes],
    stored: str | None,
    named: str,
) -> None:
    """The failure code alone was the question, not the answer.

    `PROCESS_EXITED_UNSUCCESSFULLY` told an operator that a provider died and
    nothing about why, which is the whole complaint behind the runs this head
    was cut for. The reason lives on the node the attempt was running, so the
    command asks there -- and an ending nobody recorded is reported as that
    rather than as an empty reason.
    """
    with ScriptedService(
        serving_answers(
            events=event_stream(agent_failed()), node_detail=node_detail(stored)
        )
    ) as service:
        exit_code = run_command(order, service)

    reported = capsysbinary.readouterr().err.decode()
    assert exit_code == 1
    assert "PROCESS_EXITED_UNSUCCESSFULLY" in reported
    assert named in reported


def agent_failed_v3() -> str:
    """The format-3 failure the Completed twin already spoke; the command must too.

    Dropping `AgentFailedEventResourceV3` from the acted union turns this payload
    into a validation wall — the same unread history that #253 closed for
    completion.
    """

    return AgentFailedEventResourceV3(
        workflow_format_version=3,
        node_rail=node_rail(NodeState.WORKING),
        cursor=EVENT_CURSOR,
        sequence=1,
        public_run_reference=PUBLIC_RUN_REFERENCE,
        workflow_revision_hash=REVISION_HASH,
        node_id=AGENT_NODE_ID,
        node_execution_id=NODE_EXECUTION_ID,
        event_hash=EVENT_HASH,
        event="AGENT_FAILED",
        failure_code=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        reason=None,
        attempt_id=ATTEMPT_ID,
        attempt_ordinal=1,
    ).model_dump_json()


def test_a_format_3_failed_agent_attempt_is_read_as_itself(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The Completed chain has its pin; failure of the same shape had none."""
    with ScriptedService(
        serving_answers(
            start=Answer(run_resource("STARTED", None).model_dump_json().encode()),
            events=event_stream(agent_failed_v3()),
            run=Answer(run_resource("STARTED", None).model_dump_json().encode()),
        )
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    reported = printed.err.decode()
    assert (exit_code, printed.out) == (1, b"")
    assert "PROCESS_EXITED_UNSUCCESSFULLY" in reported
    assert ATTEMPT_ID in reported
    assert AGENT_NODE_ID in reported
    assert "validation error" not in reported.lower()


def test_a_run_waiting_for_input_says_which_capability_is_missing(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(
        serving_answers(events=event_stream(waiting_for_input()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"waiting" in printed.err
    assert b"answer" in printed.err
    assert b"#38" not in printed.err


def test_an_event_history_that_ends_before_the_run_does_is_refused(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers(run=started_run())) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert b"STARTED" in printed.err


def test_a_stream_that_fails_hands_the_services_own_problem_to_the_operator(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The stream's only problem channel is a frame, and it must not be dropped."""

    with ScriptedService(
        serving_answers(events=event_stream(failure=stream_failure()))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    problem = problem_resource(STREAM_FAILURE_CODE)
    reported = printed.err.decode()
    assert problem.type in reported
    assert problem.title in reported
    assert problem.detail in reported
    assert TERMINAL_HASH not in reported


def test_a_stream_that_ends_without_an_event_is_refused_rather_than_reported_as_none(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Backpressure ends the stream regularly; a completed run is no proof of it."""

    with ScriptedService(serving_answers(events=event_stream())) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert EVENT_CURSOR.encode() in printed.err
    assert TERMINAL_HASH.encode() not in printed.err


def test_a_history_that_stops_before_the_runs_latest_event_prints_no_half_output(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(
        serving_answers(run=completed_run(latest_event_cursor=LATER_EVENT_CURSOR))
    ) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (1, b"")
    assert LATER_EVENT_CURSOR.encode() in printed.err
    assert TERMINAL_HASH.encode() not in printed.err


def test_a_typed_problem_reaches_the_operator_as_the_service_wrote_it(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    refusal = problem_answer(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "https://atelier/problems/invalid-workflow-document",
        "Invalid workflow document",
        "node draft names an unreachable successor",
    )
    with ScriptedService(serving_answers(workflow_revision=refusal)) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"https://atelier/problems/invalid-workflow-document" in printed.err
    assert b"node draft names an unreachable successor" in printed.err


def test_an_answer_that_is_not_the_published_contract_is_refused_by_name(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers(start=Answer(b'{"run_id": 17}'))) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"cannot read" in printed.err


def test_a_binding_file_that_describes_no_agent_is_refused_before_anything_runs(
    tmp_path: Path, order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    (tmp_path / "writer.json").write_bytes(b'{"model": "claude-opus-4"}')

    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(order, service)
        started = service.sent("POST", RUNS_URL_PATH)

    printed = capsysbinary.readouterr()
    assert (exit_code, started) == (1, [])
    assert b"writer" in printed.err


def published_configuration_body(service: ScriptedService) -> dict[str, object]:
    return json.loads(service.sent("POST", API_PREFIX + AGENT_CONFIGURATION_PATH)[0])


def wire_capability_refusal(value: object) -> str:
    with pytest.raises(ValidationError) as raised:
        PublishAgentConfigurationRevisionRequestResource.model_validate(
            {
                "model": "claude-opus-4",
                "auth_profile_revision_hash": AUTH_PROFILE_HASH,
                "executor_revision": "claude-subscription-v1",
                "requested_capability": value,
            }
        )
    return str(raised.value.errors()[0]["msg"])


def test_a_binding_file_that_names_the_tool_capability_publishes_that_configuration(
    tmp_path: Path, order: list[str]
) -> None:
    """The binding file speaks the wire field; the answered resource carries it."""

    named = "headless_with_tools"
    (tmp_path / "writer.json").write_bytes(binding_document(requested_capability=named))
    with ScriptedService(
        serving_answers(
            agent_configuration=published_agent_configuration(
                requested_capability=named
            )
        )
    ) as service:
        exit_code = run_command(order, service)
        published = published_configuration_body(service)

    assert exit_code == 0
    assert published["requested_capability"] == named


def test_a_binding_file_that_omits_capability_publishes_the_wire_default(
    order: list[str],
) -> None:
    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(order, service)
        published = published_configuration_body(service)

    assert exit_code == 0
    assert published["requested_capability"] == WIRE_CAPABILITY_DEFAULT
    assert "requested_capability" not in json.loads(BINDING_DOCUMENT)


def test_an_unknown_requested_capability_is_refused_in_the_wires_own_words(
    tmp_path: Path, order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    unknown = "telepathic"
    (tmp_path / "writer.json").write_bytes(
        binding_document(requested_capability=unknown)
    )
    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(order, service)
        started = service.sent("POST", RUNS_URL_PATH)

    reported = capsysbinary.readouterr().err.decode()
    assert (exit_code, started) == (1, [])
    assert wire_capability_refusal(unknown) in reported


def test_no_service_at_the_named_address_is_named_instead_of_traced(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(serving_answers()) as service:
        unserved = service.url
    exit_code = main([*order, "--service", unserved])

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert unserved.encode() in printed.err


def test_an_address_that_is_not_a_served_api_is_refused(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    exit_code = main([*order, "--service", "file:///etc/passwd"])

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert b"file:///etc/passwd" in printed.err


NAME = "review-bounded-diff"
LINEAGE_ID = "b" * 64
REVISION_NUMBER = 2
BY_NAME_URL_PATH = API_PREFIX + catalog_name_path(RevisionKind.WORKFLOW, NAME)


def name_answer() -> Answer:
    return Answer(
        json.dumps(
            {
                "display_name": NAME,
                "lineage_id": LINEAGE_ID,
                "catalog_revision_hash": REVISION_HASH,
                "revision_number": REVISION_NUMBER,
            }
        ).encode()
    )


@pytest.mark.proves("one-command-answers-what-a-name-holds")
def test_a_name_is_resolved_through_the_service_and_shown_with_what_binds_it() -> None:
    with ScriptedService({("GET", BY_NAME_URL_PATH): [name_answer()]}) as service:
        resolution = resolve_published_name(NameOrder(service.url, NAME))

    assert resolution.revision_hash == REVISION_HASH
    assert resolution.revision_number == 2
    shown = describe_resolution(resolution)
    assert NAME in shown
    assert REVISION_HASH in shown
    assert "2" in shown


@pytest.mark.proves("one-command-answers-what-a-name-holds")
def test_resolving_a_name_starts_nothing() -> None:
    with ScriptedService({("GET", BY_NAME_URL_PATH): [name_answer()]}) as service:
        resolve_published_name(NameOrder(service.url, NAME))

        assert service.sent("POST", RUNS_URL_PATH) == []
        assert [call.method for call in service.calls] == ["GET"]


def test_a_position_is_asked_of_the_service_rather_than_chosen_here() -> None:
    path = f"{BY_NAME_URL_PATH}?position=1"
    with ScriptedService({("GET", path): [name_answer()]}) as service:
        resolve_published_name(NameOrder(service.url, NAME, position="1"))

        assert [call.path for call in service.calls] == [path]


@pytest.mark.proves("a-refused-name-ends-the-command-unsuccessfully")
def test_a_name_the_service_refuses_is_handed_on_in_its_own_words() -> None:
    refused = problem_answer(
        HTTPStatus.NOT_FOUND,
        "urn:atelier2:problem:v1:catalog-name-not-found",
        "Catalog name not found",
        "No lineage of this kind holds that name at that position.",
    )
    with (
        ScriptedService({("GET", BY_NAME_URL_PATH): [refused]}) as service,
        pytest.raises(ServiceRefused) as refusal,
    ):
        resolve_published_name(NameOrder(service.url, NAME))

    assert "catalog-name-not-found" in str(refusal.value)
    assert "No lineage of this kind holds that name" in str(refusal.value)


@pytest.mark.proves("one-command-answers-what-a-name-holds")
def test_the_command_shows_what_the_name_holds_and_ends_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with ScriptedService({("GET", BY_NAME_URL_PATH): [name_answer()]}) as service:
        exit_code = main(["resolve", "--name", NAME, "--service", service.url])

    shown = capsys.readouterr()
    # One line, and every value in it distinct: a name, a lineage id, a member
    # number and a revision hash that cannot stand in for one another. Asserting
    # the whole line is what makes a swapped or dropped field fail here rather
    # than read plausibly to an operator.
    assert exit_code == 0
    assert shown.out == (
        f"{NAME} is revision {REVISION_NUMBER} "
        f"of lineage {LINEAGE_ID}: {REVISION_HASH}\n"
    )
    assert shown.err == ""


@pytest.mark.proves("a-refused-name-ends-the-command-unsuccessfully")
def test_a_refused_name_ends_the_command_unsuccessfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = problem_answer(
        HTTPStatus.GONE,
        "urn:atelier2:problem:v1:catalog-lineage-retired",
        "Catalog lineage retired",
        "This name was retired; it resolves to no revision a run may use.",
    )
    with ScriptedService({("GET", BY_NAME_URL_PATH): [refused]}) as service:
        exit_code = main(["resolve", "--name", NAME, "--service", service.url])

    shown = capsys.readouterr()
    assert exit_code == 1
    assert shown.out == ""
    assert "catalog-lineage-retired" in shown.err


def named_serving_answers() -> dict[tuple[str, str], list[Answer]]:
    """The conversation of a run started by name: resolve, then run.

    No workflow revision is published, because the name already holds one. The
    absence of that answer is what proves it: a command that still published
    would meet a service with nothing scripted for it.
    """

    answers = serving_answers()
    del answers[("POST", API_PREFIX + WORKFLOW_REVISION_PATH)]
    answers[("GET", BY_NAME_URL_PATH)] = [name_answer()]
    return answers


@pytest.fixture
def named_order(tmp_path: Path) -> Iterator[list[str]]:
    binding = tmp_path / "writer.json"
    binding.write_bytes(BINDING_DOCUMENT)
    yield ["run", "--name", NAME, "--binding", f"{AGENT_ROLE}={binding}"]


@pytest.mark.proves("one-command-runs-the-workflow-a-name-holds")
def test_a_named_workflow_runs_and_prints_the_output_of_the_run_it_started(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The command #111 exists for: a name in, the run's own output out.

    Until the runtime could execute a published revision end to end, a `run` that
    resolved a name would have been a verb that lies. It starts now, so the
    resolution and the run are one command rather than two and a copied hash.
    """
    with ScriptedService(named_serving_answers()) as service:
        exit_code = run_command(named_order, service)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])
        published_workflows = service.sent("POST", API_PREFIX + WORKFLOW_REVISION_PATH)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)
    assert published_workflows == []
    assert started["workflow_revision_hash"] == REVISION_HASH
    reported = printed.err.decode()
    assert PUBLIC_RUN_REFERENCE in reported
    assert TERMINAL_HASH in reported
    assert NAME in reported


@pytest.mark.proves("one-command-runs-the-workflow-a-name-holds")
def test_a_named_run_asks_the_service_for_the_name_before_it_starts_anything(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Resolution is a question, and it is asked before any run exists."""
    with ScriptedService(named_serving_answers()) as service:
        run_command(named_order, service)
        asked = [(call.method, call.path) for call in service.calls]

    assert asked[0] == ("GET", BY_NAME_URL_PATH)
    assert asked.index(("POST", RUNS_URL_PATH)) > asked.index(("GET", BY_NAME_URL_PATH))


@pytest.mark.proves("a-refused-name-ends-the-command-unsuccessfully")
def test_a_name_the_service_refuses_starts_no_run_and_ends_unsuccessfully(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    refused = problem_answer(
        HTTPStatus.NOT_FOUND,
        "urn:atelier2:problem:v1:catalog-name-not-found",
        "Catalog name not found",
        "No lineage of this kind holds that name at that position.",
    )
    answers = named_serving_answers()
    answers[("GET", BY_NAME_URL_PATH)] = [refused]
    with ScriptedService(answers) as service:
        exit_code = run_command(named_order, service)
        started = service.sent("POST", RUNS_URL_PATH)

    assert (exit_code, started) == (1, [])
    assert "catalog-name-not-found" in capsysbinary.readouterr().err.decode()


def test_a_run_names_either_a_document_or_a_name_and_never_both(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Two sources for the same revision would leave the operator guessing."""
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)

    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--workflow",
                str(workflow),
                "--name",
                NAME,
                "--service",
                "http://x",
            ]
        )

    assert b"--name" in capsysbinary.readouterr().err


def test_a_run_that_names_no_workflow_at_all_is_refused_before_any_request(
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--service", "http://x"])

    assert b"--workflow" in capsysbinary.readouterr().err


def test_a_position_without_a_name_is_refused_rather_than_ignored(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Reading an option and then ignoring it is a quiet disagreement."""
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(WORKFLOW_DOCUMENT)

    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--workflow",
                str(workflow),
                "--position",
                "2",
                "--service",
                "http://x",
            ]
        )

    assert b"--position" in capsysbinary.readouterr().err


@pytest.mark.proves("one-command-runs-the-workflow-a-name-holds")
def test_a_named_run_at_an_exact_member_asks_the_service_for_that_member(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """A name can be run at the member the operator meant, not only at its head."""
    path = f"{BY_NAME_URL_PATH}?position=1"
    answers = named_serving_answers()
    del answers[("GET", BY_NAME_URL_PATH)]
    answers[("GET", path)] = [name_answer()]
    with ScriptedService(answers) as service:
        exit_code = run_command(named_order, service, "--position", "1")
        asked = [call.path for call in service.calls]

    assert exit_code == 0
    assert path in asked


@pytest.mark.proves("one-command-runs-the-workflow-a-name-holds")
def test_the_command_reads_the_events_of_the_chain_it_started(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The whole point of the command, for the runs this workshop is built for.

    A format-3 line answers in the shape #249 landed, and this command decoded
    only V1 and V2 -- so the first live chain run ended with 46 validation errors
    and exit 1 while the run itself completed cleanly on the server. The exit-0
    contract says the history was read whole and the terminal was seen; a chain
    is exactly the run for which that promise matters most.
    """
    with ScriptedService(chained_serving_answers()) as service:
        exit_code = run_command(order, service)

    printed = capsysbinary.readouterr()
    # Piped output is the output: every node's work, in the order it was done.
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT + CHAIN_SECOND_OUTPUT)
    reported = printed.err.decode()
    assert TERMINAL_HASH in reported
    assert CHAIN_SECOND_NODE_ID in reported


ORDER_NAME = "order"
ORDER_VALUE = '{"portions": 4}'


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_a_named_run_publishes_its_order_as_an_artifact_before_the_start(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """`--name` plus `--input` is one run of the named revision, not a new one.

    The order's exact bytes reach `POST /artifacts` before the start ever
    happens, and `POST /runs` names only the address the artifact door
    answered -- never the bytes themselves.
    """
    with ScriptedService(named_serving_answers()) as service:
        exit_code = run_command(
            named_order, service, "--input", f"{ORDER_NAME}={ORDER_VALUE}"
        )
        called_paths = [call.path for call in service.calls]
        published_artifacts = service.sent("POST", ARTIFACTS_URL_PATH)
        artifact_call = next(
            call for call in service.calls if call.path == ARTIFACTS_URL_PATH
        )
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])
        published_workflows = service.sent("POST", API_PREFIX + WORKFLOW_REVISION_PATH)

    printed = capsysbinary.readouterr()
    assert (exit_code, printed.out) == (0, AGENT_OUTPUT)
    assert published_workflows == []
    assert published_artifacts == [ORDER_VALUE.encode()]
    assert artifact_call.content_type == OCTET_STREAM_MEDIA_TYPE
    assert called_paths.index(ARTIFACTS_URL_PATH) < called_paths.index(RUNS_URL_PATH)
    assert started["workflow_revision_hash"] == REVISION_HASH
    assert started["workflow_format_version"] == 3
    assert started["orders"] == [{"name": ORDER_NAME, "artifact_hash": ARTIFACT_HASH}]


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_a_document_run_publishes_its_order_the_same_way(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """`--workflow` is the other door; the order still publishes before the start."""
    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(
            order, service, "--input", f"{ORDER_NAME}={ORDER_VALUE}"
        )
        published_artifacts = service.sent("POST", ARTIFACTS_URL_PATH)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])

    assert (exit_code, capsysbinary.readouterr().out) == (0, AGENT_OUTPUT)
    assert published_artifacts == [ORDER_VALUE.encode()]
    assert started["orders"] == [{"name": ORDER_NAME, "artifact_hash": ARTIFACT_HASH}]


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_an_input_file_publishes_the_exact_bytes_it_held(
    named_order: list[str],
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    order_file = tmp_path / "order.json"
    order_file.write_bytes(ORDER_VALUE.encode())

    with ScriptedService(named_serving_answers()) as service:
        exit_code = run_command(
            named_order, service, "--input-file", f"{ORDER_NAME}={order_file}"
        )
        published_artifacts = service.sent("POST", ARTIFACTS_URL_PATH)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])

    assert (exit_code, capsysbinary.readouterr().out) == (0, AGENT_OUTPUT)
    assert published_artifacts == [ORDER_VALUE.encode()]
    assert started["orders"] == [{"name": ORDER_NAME, "artifact_hash": ARTIFACT_HASH}]


@pytest.mark.proves("a-full-pull-request-diff-reaches-its-agent-as-an-artifact")
def test_an_input_larger_than_the_old_inline_bound_still_publishes(
    named_order: list[str],
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    """A ~100 KB diff took the same door a small order does; no size branch.

    `MAXIMUM_INSTANCE_DOCUMENT_BYTES` (16 KiB) used to bound what an inline
    order could carry; this order is well past it, and the command still
    hands it to `POST /artifacts` and starts with only the address.
    """
    large_order = json.dumps({"diff": "x" * 100_000}).encode()
    order_file = tmp_path / "diff.json"
    order_file.write_bytes(large_order)

    with ScriptedService(named_serving_answers()) as service:
        exit_code = run_command(
            named_order, service, "--input-file", f"{ORDER_NAME}={order_file}"
        )
        published_artifacts = service.sent("POST", ARTIFACTS_URL_PATH)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])

    assert (exit_code, capsysbinary.readouterr().out) == (0, AGENT_OUTPUT)
    assert published_artifacts == [large_order]
    assert started["orders"] == [{"name": ORDER_NAME, "artifact_hash": ARTIFACT_HASH}]


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_two_inputs_each_publish_their_own_artifact(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    answers = named_serving_answers()
    answers[("POST", ARTIFACTS_URL_PATH)] = [
        published_artifact(ARTIFACT_HASH),
        published_artifact(SECOND_ARTIFACT_HASH),
    ]
    with ScriptedService(answers) as service:
        exit_code = run_command(
            named_order,
            service,
            "--input",
            f"{ORDER_NAME}={ORDER_VALUE}",
            "--input",
            'side={"name": "beans"}',
        )
        published_artifacts = service.sent("POST", ARTIFACTS_URL_PATH)
        started = json.loads(service.sent("POST", RUNS_URL_PATH)[0])
        published_workflows = service.sent("POST", API_PREFIX + WORKFLOW_REVISION_PATH)

    assert (exit_code, published_workflows) == (0, [])
    assert published_artifacts == [ORDER_VALUE.encode(), b'{"name": "beans"}']
    assert started["orders"] == [
        {"name": ORDER_NAME, "artifact_hash": ARTIFACT_HASH},
        {"name": "side", "artifact_hash": SECOND_ARTIFACT_HASH},
    ]
    assert capsysbinary.readouterr().out == AGENT_OUTPUT


def test_a_value_that_is_not_json_is_refused_before_any_request(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with (
        ScriptedService(named_serving_answers()) as service,
        pytest.raises(SystemExit),
    ):
        run_command(named_order, service, "--input", f"{ORDER_NAME}=not-json")

    printed = capsysbinary.readouterr()
    assert b"not valid JSON for the pinned schema" in printed.err
    assert b"order" in printed.err


def test_an_input_file_that_is_not_utf8_is_refused_by_name(
    named_order: list[str],
    tmp_path: Path,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    """`json.loads` on bytes decodes UTF-8 first; latin-1 is not a JSON error.

    A file of `{"greeting": "grüße"}` in latin-1 raises `UnicodeDecodeError`,
    which is not a `JSONDecodeError`. That used to escape as a traceback.
    """
    order_file = tmp_path / "order.json"
    order_file.write_bytes('{"greeting": "grüße"}'.encode("latin-1"))

    with (
        ScriptedService(named_serving_answers()) as service,
        pytest.raises(SystemExit),
    ):
        run_command(named_order, service, "--input-file", f"{ORDER_NAME}={order_file}")

    printed = capsysbinary.readouterr()
    assert b"not valid JSON for the pinned schema" in printed.err
    assert b"order" in printed.err


def test_an_input_that_is_not_a_name_assignment_is_refused(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with (
        ScriptedService(named_serving_answers()) as service,
        pytest.raises(SystemExit),
    ):
        run_command(named_order, service, "--input", ORDER_VALUE)

    assert b"NAME=VALUE" in capsysbinary.readouterr().err


def test_the_same_input_twice_is_refused_before_any_request(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with (
        ScriptedService(named_serving_answers()) as service,
        pytest.raises(SystemExit),
    ):
        run_command(
            named_order,
            service,
            "--input",
            f"{ORDER_NAME}={ORDER_VALUE}",
            "--input",
            f"{ORDER_NAME}=" + '{"portions": 9}',
        )

    printed = capsysbinary.readouterr()
    assert b"supplied twice" in printed.err
    assert ORDER_NAME.encode() in printed.err


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
def test_a_run_input_refusal_reaches_the_operator_as_the_service_wrote_it(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The command must not translate a 422 into a friendlier sentence."""
    detail = "input 'order' was refused: undeclared"
    refusal = problem_answer(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "urn:atelier2:problem:v1:run-input-refused",
        "Run input refused",
        detail,
    )
    answers = named_serving_answers()
    answers[("POST", RUNS_URL_PATH)] = [refusal]
    with ScriptedService(answers) as service:
        exit_code = run_command(
            named_order, service, "--input", f"{ORDER_NAME}={ORDER_VALUE}"
        )
        started = service.sent("POST", RUNS_URL_PATH)

    printed = capsysbinary.readouterr()
    assert exit_code == 1
    assert started  # the command did ask; the service named the refusal
    reported = printed.err.decode()
    assert "urn:atelier2:problem:v1:run-input-refused" in reported
    assert detail in reported


def test_different_orders_ask_for_different_runs(
    named_order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with ScriptedService(named_serving_answers()) as service:
        run_command(named_order, service, "--input", f"{ORDER_NAME}={ORDER_VALUE}")
        run_command(
            named_order, service, "--input", f"{ORDER_NAME}=" + '{"portions": 9}'
        )
        started = [
            json.loads(body)["run_id"] for body in service.sent("POST", RUNS_URL_PATH)
        ]

    assert started[0] != started[1]
    assert capsysbinary.readouterr().out == AGENT_OUTPUT + AGENT_OUTPUT


def test_the_run_help_describes_input_instead_of_deferring_it(
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    with pytest.raises(SystemExit) as ended:
        main(["run", "--help"])

    assert ended.value.code == 0
    shown = capsysbinary.readouterr().out.decode()
    assert "--input" in shown
    assert "--input-file" in shown
    assert "follows issue #38" not in shown


ILLEGAL_V3_NAME = "Der erste Lauf auf V14"
ILLEGAL_V3_DOCUMENT = b"""format_version: 3
name: Der erste Lauf auf V14
nodes:
  - id: draft
    type: agent
    role: writer
    mode: headless
    instruction: The first live V14 run.
"""
LINEAGES_URL_PATH = API_PREFIX + CATALOG_LINEAGE_PATH
MEMBERS_URL_PATH = f"{LINEAGES_URL_PATH}/{LINEAGE_ID}/members"
V3_BY_NAME_URL_PATH = API_PREFIX + catalog_name_path(
    RevisionKind.WORKFLOW, WORKFLOW_NAME
)


def published_workflow_revision(name: str = WORKFLOW_NAME) -> Answer:
    document = WORKFLOW_DOCUMENT if name == WORKFLOW_NAME else ILLEGAL_V3_DOCUMENT
    return Answer(
        WorkflowRevisionDetailResource(
            workflow_revision_hash=REVISION_HASH,
            document_base64=encode_canonical_base64(document),
            graph=WorkflowGraphResourceV3(
                workflow_format_version=3,
                executable=True,
                not_executable_reason=None,
                node_count=1,
                agent_roles=("writer",),
                orders=(),
                wait_answer_schemas=(),
                node_previews=(
                    WorkflowNodePreviewResourceV3(
                        id="draft",
                        kind="agent",
                        role="writer",
                        instruction_start="Review the bound diff.",
                        depends_on=(),
                    ),
                ),
                loops=(),
                name=name,
                description="Review a bound diff." if name == WORKFLOW_NAME else None,
            ),
        )
        .model_dump_json()
        .encode()
    )


def founded_lineage(revision_number: int = 1) -> Answer:
    return Answer(
        CatalogAdmissionResource(
            display_name=WORKFLOW_NAME,
            lineage_id=LINEAGE_ID,
            catalog_revision_hash=REVISION_HASH,
            revision_number=revision_number,
        )
        .model_dump_json()
        .encode(),
        status=HTTPStatus.CREATED,
    )


@pytest.mark.proves("a-cli-published-v3-workflow-is-named-and-then-run-by-that-name")
def test_publishing_a_v3_document_names_it_through_the_admission_door(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Publication stays POST /workflow-revisions; naming is the second act."""
    with ScriptedService(serving_answers()) as service:
        exit_code = run_command(order, service)
        founded = json.loads(service.sent("POST", LINEAGES_URL_PATH)[0])
        asked = [(call.method, call.path) for call in service.calls]

    assert exit_code == 0
    assert founded == {
        "kind": RevisionKind.WORKFLOW.value,
        "catalog_revision_hash": REVISION_HASH,
        "actor": COMMAND_CATALOG_ACTOR,
        "activated_at": founded["activated_at"],
    }
    assert founded["activated_at"].endswith("Z")
    assert "T" in founded["activated_at"]
    assert asked.index(("POST", LINEAGES_URL_PATH)) > asked.index(
        ("POST", API_PREFIX + WORKFLOW_REVISION_PATH)
    )
    assert asked.index(("POST", RUNS_URL_PATH)) > asked.index(
        ("POST", LINEAGES_URL_PATH)
    )
    assert capsysbinary.readouterr().out == AGENT_OUTPUT


@pytest.mark.proves("a-cli-published-v3-workflow-is-named-and-then-run-by-that-name")
def test_a_named_run_starts_the_revision_the_just_published_name_holds(
    order: list[str],
    named_order: list[str],
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    answers = serving_answers()
    answers[("GET", V3_BY_NAME_URL_PATH)] = [
        Answer(
            json.dumps(
                {
                    "display_name": WORKFLOW_NAME,
                    "lineage_id": LINEAGE_ID,
                    "catalog_revision_hash": REVISION_HASH,
                    "revision_number": 1,
                }
            ).encode()
        )
    ]
    with ScriptedService(answers) as service:
        published = run_command(order, service)
        named = run_command(["run", "--name", WORKFLOW_NAME, *named_order[3:]], service)
        published_again = service.sent("POST", API_PREFIX + WORKFLOW_REVISION_PATH)

    assert (published, named) == (0, 0)
    assert len(published_again) == 1
    assert capsysbinary.readouterr().out == AGENT_OUTPUT + AGENT_OUTPUT


def test_an_illegal_catalog_name_still_starts_and_founds_nothing(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes(ILLEGAL_V3_DOCUMENT)
    binding = tmp_path / "writer.json"
    binding.write_bytes(BINDING_DOCUMENT)
    answers = serving_answers(
        workflow_revision=published_workflow_revision(ILLEGAL_V3_NAME)
    )
    with ScriptedService(answers) as service:
        exit_code = run_command(
            [
                "run",
                "--workflow",
                str(workflow),
                "--binding",
                f"{AGENT_ROLE}={binding}",
            ],
            service,
        )
        founded = service.sent("POST", LINEAGES_URL_PATH)

    assert (exit_code, founded) == (0, [])
    assert capsysbinary.readouterr().out == AGENT_OUTPUT


def test_an_admission_invalid_request_is_a_named_refusal_and_starts_nothing(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """A founding invalid-request is not the catalog-name skip.

    The grammar skip happens locally before POST. Any other invalid-request
    from the admission door is the service naming a refusal, and starting
    by hash would hide it.
    """
    problem = problem_resource(
        "invalid-request", "activated_at is not a catalog activation instant"
    )
    answers = serving_answers()
    answers[("POST", LINEAGES_URL_PATH)] = [
        Answer(
            problem.model_dump_json().encode(),
            status=HTTPStatus(problem.status),
            media_type=PROBLEM_MEDIA_TYPE,
        )
    ]
    with ScriptedService(answers) as service:
        exit_code = run_command(order, service)
        founded = service.sent("POST", LINEAGES_URL_PATH)
        started = service.sent("POST", RUNS_URL_PATH)

    printed = capsysbinary.readouterr()
    reported = printed.err.decode()
    assert founded
    assert (exit_code, started, printed.out) == (1, [], b"")
    assert problem.type in reported
    assert problem.detail in reported


def test_an_already_owned_revision_skips_founding_and_still_starts(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    problem = problem_resource("catalog-revision-owned")
    answers = serving_answers()
    answers[("POST", LINEAGES_URL_PATH)] = [
        Answer(
            problem.model_dump_json().encode(),
            status=HTTPStatus(problem.status),
            media_type=PROBLEM_MEDIA_TYPE,
        )
    ]
    with ScriptedService(answers) as service:
        exit_code = run_command(order, service)
        started = service.sent("POST", RUNS_URL_PATH)
        members = service.sent("POST", MEMBERS_URL_PATH)

    assert (exit_code, members) == (0, [])
    assert started
    assert capsysbinary.readouterr().out == AGENT_OUTPUT


def test_a_held_name_admits_the_new_revision_into_that_lineage(
    order: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    answers = serving_answers()
    answers[("POST", LINEAGES_URL_PATH)] = [
        problem_answer(
            HTTPStatus.CONFLICT,
            "urn:atelier2:problem:v1:catalog-name-held",
            "Catalog name is held",
            "Another lineage already holds that name.",
        )
    ]
    answers[("GET", V3_BY_NAME_URL_PATH)] = [
        Answer(
            json.dumps(
                {
                    "display_name": WORKFLOW_NAME,
                    "lineage_id": LINEAGE_ID,
                    "catalog_revision_hash": REVISION_HASH,
                    "revision_number": 1,
                }
            ).encode()
        )
    ]
    answers[("POST", MEMBERS_URL_PATH)] = [founded_lineage(revision_number=2)]
    with ScriptedService(answers) as service:
        exit_code = run_command(order, service)
        members = json.loads(service.sent("POST", MEMBERS_URL_PATH)[0])

    assert exit_code == 0
    assert members == {
        "kind": RevisionKind.WORKFLOW.value,
        "catalog_revision_hash": REVISION_HASH,
        "actor": COMMAND_CATALOG_ACTOR,
        "activated_at": members["activated_at"],
    }
    assert capsysbinary.readouterr().out == AGENT_OUTPUT
