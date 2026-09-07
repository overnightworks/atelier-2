from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.api.app import create_app
from atelier2.api.limits import ApiLimitExceeded, ApiLimits, RequestBodyLimitMiddleware
from atelier2.api.openapi import (
    API_PREFIX,
    LIBRARY_ADDITIONS_PATH,
    LIBRARY_RECOGNITIONS_PATH,
)
from atelier2.api.references import (
    encode_canonical_base64,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionRequestHash,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageFounded,
)
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    AgentNodeRefusalRecord,
    NodeExecutionId,
    RunEvent,
    RunEventKind,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import (
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.host.serving import api_limits as deployed_api_limits
from atelier2.ports.catalog_intakes import CatalogIntakeStored
from atelier2.ports.durable_runs import (
    DurableAnswerRunMissing,
    DurableRunRevisionMissing,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCreated,
)
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff


@dataclass
class RecordingMutationPorts:
    publications: list[WorkflowRevision] = field(default_factory=list)
    additions: list[WorkflowRevision] = field(default_factory=list)
    starts: list[object] = field(default_factory=list)
    answers: list[object] = field(default_factory=list)

    def publish(self, revision: WorkflowRevision) -> DurableRevisionCreated:
        self.publications.append(revision)
        return DurableRevisionCreated(revision)

    def add_workflow(
        self,
        revision: WorkflowRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
    ) -> CatalogLineageFounded:
        del actor, activated_at
        self.additions.append(revision)
        published = PublishedRevision(RevisionKind.WORKFLOW, revision.document)
        return CatalogLineageFounded(
            CatalogLineage(published.kind, published.revision_hash),
            published,
            display_name,
        )

    def store_intake(self, intake: object) -> object:
        return CatalogIntakeStored(cast(Any, intake))

    def start_published(self, request: object) -> DurableRunRevisionMissing:
        self.starts.append(request)
        return DurableRunRevisionMissing()

    def submit_result(self, request: object) -> DurableAnswerRunMissing:
        self.answers.append(request)
        return DurableAnswerRunMissing()


def client_for(mutations: RecordingMutationPorts, limits: ApiLimits) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                workflow_revision_publisher=mutations,
                published_run_starter=mutations,
                wait_answerer=mutations,
                library_additions=mutations,
                catalog_intakes=mutations,
                workflow_document_parser=parse_executable_workflow_document,
                agent_definition_parser=parse_agent_definition,
                agent_definition_renderer=render_agent_definition,
            ),
            limits=limits,
            event_poll_backoff=event_poll_backoff(),
        )
    )


def test_agent_attempt_public_fields_use_the_shared_field_bound() -> None:
    """The attempt id the wire publishes is measured, minted by its own owner.

    Derived rather than spelled: an attempt id is whatever `AgentAttemptId`
    mints, so a host that lowered the field bound below that width refuses it
    however wide the digest becomes.
    """

    limits = api_limits(maximum_field_characters=63)
    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId.for_node(
            RunId("attempt/api"), WorkflowRevisionHash("a" * 64), "build"
        ),
        AgentExecutionRequestHash("1" * 64),
    )

    with pytest.raises(ApiLimitExceeded):
        limits.require_field(attempt_id.value)


def workflow_document(*, job: str = "work", include_agent: bool = False) -> bytes:
    """A publishable document; `include_agent` is the second node past a node bound."""

    second_node = (
        "  - id: check\n"
        "    type: agent\n"
        "    role: reviewer\n"
        "    mode: headless\n"
        f"    instruction: {job}\n"
        "    depends_on: [draft]\n"
        "    inputs:\n"
        "      - name: candidate\n"
        "        from: {node: draft, output: candidate}\n"
        "    outputs:\n"
        "      - name: findings\n"
        "        schema: {ref: review_verdict, revision: schema-verdict}\n"
    )
    return (
        "format_version: 3\n"
        "name: bounded-document\n"
        "nodes:\n"
        "  - id: draft\n"
        "    type: agent\n"
        "    role: builder\n"
        "    mode: headless\n"
        f"    instruction: {job}\n"
        "    outputs:\n"
        "      - name: candidate\n"
        "        schema: {ref: workspace_candidate, revision: schema-candidate}\n"
        + (second_node if include_agent else "")
    ).encode()


def assert_problem(response: Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith(":" + code)


def test_api_limits_reject_base64_capacity_that_cannot_encode_a_payload() -> None:
    with pytest.raises(ValueError, match="maximum_base64_characters"):
        api_limits(maximum_base64_characters=3)

    limits = api_limits(maximum_base64_characters=4)
    assert limits.maximum_base64_decoded_bytes == 3
    client_for(RecordingMutationPorts(), limits)


@pytest.mark.parametrize(
    ("limits", "payload", "detail"),
    [
        (
            api_limits(maximum_decoded_payload_bytes=2),
            b"123",
            "decoded payload exceeds its byte limit",
        ),
        (
            api_limits(
                maximum_base64_characters=4,
                maximum_decoded_payload_bytes=4,
            ),
            b"1234",
            "encoded payload exceeds its character limit",
        ),
    ],
)
def test_encoded_projection_limit_branches_are_explicit(
    limits: ApiLimits, payload: bytes, detail: str
) -> None:
    with pytest.raises(ValueError, match=detail):
        limits.require_encoded_payload(payload)


@pytest.mark.parametrize(
    ("path", "problem_code"),
    [
        (API_PREFIX + "/workflow-revisions", "invalid-workflow-document"),
        (API_PREFIX + "/runs", "invalid-request"),
        (LIBRARY_RECOGNITIONS_PATH, "invalid-request"),
        (LIBRARY_ADDITIONS_PATH, "invalid-request"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"one")],
        [(b"content-length", b"-1")],
    ],
)
def test_body_limit_rejects_noncanonical_content_length_directly(
    path: str,
    problem_code: str,
    headers: list[tuple[bytes, bytes]],
) -> None:
    async def scenario() -> tuple[int, bytes, bool]:
        reached_route = False
        messages: list[Message] = []

        async def route(_scope: Scope, _receive: Receive, send: Send) -> None:
            nonlocal reached_route
            reached_route = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = RequestBodyLimitMiddleware(
            cast(ASGIApp, route),
            maximum_body_bytes=4,
            api_prefix=API_PREFIX,
        )

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            messages.append(message)

        await middleware(
            cast(
                Scope,
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "headers": headers,
                },
            ),
            receive,
            send,
        )
        start = messages[0]
        body = messages[1]
        return int(start["status"]), cast(bytes, body["body"]), reached_route

    status, body, reached_route = asyncio.run(scenario())

    assert status == 422
    assert problem_code.encode() in body
    assert not reached_route


def test_body_limit_bypasses_routes_that_do_not_read_a_body() -> None:
    async def scenario() -> tuple[int, bool]:
        reached_route = False
        messages: list[Message] = []

        async def route(_scope: Scope, _receive: Receive, send: Send) -> None:
            nonlocal reached_route
            reached_route = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = RequestBodyLimitMiddleware(
            cast(ASGIApp, route),
            maximum_body_bytes=4,
            api_prefix=API_PREFIX,
        )

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            messages.append(message)

        await middleware(
            cast(
                Scope,
                {
                    "type": "http",
                    "method": "GET",
                    "path": API_PREFIX + "/health",
                    "headers": [(b"content-length", b"invalid")],
                },
            ),
            receive,
            send,
        )
        return int(messages[0]["status"]), reached_route

    assert asyncio.run(scenario()) == (204, True)


def test_yaml_body_limit_accepts_exact_bytes_and_rejects_one_more_before_write() -> (
    None
):
    mutations = RecordingMutationPorts()
    document = workflow_document()
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(document)),
    )

    exact = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )
    oversized = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document + b" ",
        headers={"content-type": "application/yaml"},
    )

    assert exact.status_code == 201
    assert_problem(oversized, 422, "invalid-workflow-document")
    assert mutations.publications == [WorkflowRevision(document)]


def test_recognition_body_limit_rejects_one_byte_more_than_the_envelope() -> None:
    document = workflow_document()
    client = client_for(
        RecordingMutationPorts(),
        api_limits(maximum_request_body_bytes=len(document)),
    )

    exact = client.post(
        LIBRARY_RECOGNITIONS_PATH,
        content=document,
        headers={"content-type": "application/octet-stream"},
    )
    oversized = client.post(
        LIBRARY_RECOGNITIONS_PATH,
        content=document + b" ",
        headers={"content-type": "application/octet-stream"},
    )

    assert exact.status_code == 200
    assert_problem(oversized, 422, "invalid-request")


def named_workflow_document() -> bytes:
    """A workflow the library will take, so only the envelope can turn it away."""

    return (
        b"format_version: 3\n"
        b"name: review-bounded-diff\n"
        b"nodes:\n"
        b"  - id: review\n"
        b"    type: agent\n"
        b"    role: reviewer\n"
        b"    mode: headless\n"
        b"    instruction: Review one bounded diff.\n"
        b"    outputs:\n"
        b"      - name: findings\n"
        b"        schema: {ref: review_verdict, revision: schema-verdict}\n"
    )


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
def test_addition_body_limit_rejects_one_byte_more_than_the_envelope() -> None:
    """The envelope refuses before the one act that would publish and admit."""

    document = named_workflow_document()
    mutations = RecordingMutationPorts()
    client = client_for(mutations, api_limits(maximum_request_body_bytes=len(document)))
    attribution = {
        "kind": "workflow",
        "actor": "operator",
        "activated_at": "2026-08-26T00:00:00Z",
    }

    exact = client.post(
        LIBRARY_ADDITIONS_PATH,
        content=document,
        params=attribution,
        headers={"content-type": "application/octet-stream"},
    )
    oversized = client.post(
        LIBRARY_ADDITIONS_PATH,
        content=document + b" ",
        params=attribution,
        headers={"content-type": "application/octet-stream"},
    )

    assert exact.status_code == 201, exact.text
    assert_problem(oversized, 422, "invalid-request")
    assert mutations.additions == []


def test_missing_content_length_is_bounded_while_receiving_chunks() -> None:
    mutations = RecordingMutationPorts()
    document = workflow_document()
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(document)),
    )

    response = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=iter((document, b" ")),
        headers={"content-type": "application/yaml"},
    )

    assert "content-length" not in response.request.headers
    assert_problem(response, 422, "invalid-workflow-document")
    assert response.json()["detail"] == "Request body exceeds its byte limit."
    assert mutations.publications == []


def test_declared_and_chunked_body_overflow_have_the_same_exact_detail() -> None:
    mutations = RecordingMutationPorts()
    document = workflow_document()
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(document)),
    )

    declared = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document + b" ",
        headers={"content-type": "application/yaml"},
    )
    chunked = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=iter((document, b" ")),
        headers={"content-type": "application/yaml"},
    )

    assert declared.status_code == chunked.status_code == 422
    assert (
        declared.json()["detail"]
        == chunked.json()["detail"]
        == ("Request body exceeds its byte limit.")
    )


def test_overlong_decoded_reference_and_cursor_keep_their_existing_codes() -> None:
    mutations = RecordingMutationPorts()
    client = client_for(mutations, api_limits(maximum_field_characters=12))
    overlong_reference = encode_public_run_reference(RunId("1234567890123"))
    valid_reference = encode_public_run_reference(RunId("run"))

    reference = client.get(f"/atelier/api/v1/runs/{overlong_reference}")
    cursor = client.get(
        f"/atelier/api/v1/runs/{valid_reference}/events",
        headers={
            "accept": "text/event-stream",
            "last-event-id": "event1.cnVu.123",
        },
    )
    attention_cursor = client.get(
        "/atelier/api/v1/events",
        headers={
            "accept": "text/event-stream",
            "last-event-id": "event1.cnVu.123",
        },
    )

    assert_problem(reference, 400, "invalid-public-run-reference")
    assert_problem(cursor, 400, "invalid-event-cursor")
    assert_problem(attention_cursor, 400, "invalid-event-cursor")


def test_public_reference_is_bounded_before_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = RecordingMutationPorts()
    client = client_for(mutations, api_limits(maximum_field_characters=12))

    def unexpected_decode(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("over-limit public reference reached base64 decoding")

    monkeypatch.setattr("atelier2.api.references.base64.b64decode", unexpected_decode)

    response = client.get("/atelier/api/v1/runs/run1." + "a" * 13)

    assert_problem(response, 400, "invalid-public-run-reference")


def test_wire_reference_and_cursor_are_bounded_by_their_own_encoding() -> None:
    document = workflow_document()
    revision = WorkflowRevision(document)
    graph = parse_executable_workflow_document(document)
    run = Run(
        RunId("x"),
        revision.revision_hash,
        RunState.STARTED,
        "final",
        0,
        1,
    )
    projection = RunProjection(run, graph, None)
    event = PersistedRunEvent(
        RunEvent(
            run.run_id,
            run.revision_hash,
            1,
            "final",
            NodeExecutionId.for_node(run.run_id, run.revision_hash, "final"),
            RunEventKind.SUBWORKFLOW_COMPLETED,
            b"3",
        ),
        None,
    )
    reference = encode_public_run_reference(run.run_id)
    cursor = encode_event_cursor(run.run_id, 1)
    assert len(reference) < len(cursor)

    with pytest.raises(ApiLimitExceeded, match="public run reference"):
        api_limits(maximum_field_characters=len(reference) - 1).require_run_projection(
            projection
        )
    with pytest.raises(ApiLimitExceeded, match="event cursor"):
        api_limits(maximum_field_characters=len(cursor) - 1).require_event_projection(
            event
        )


def _refused_before_any_attempt(detail: str) -> PersistedRunEvent:
    revision = WorkflowRevision(workflow_document())
    run_id = RunId("refused")
    return PersistedRunEvent(
        RunEvent(
            run_id,
            revision.revision_hash,
            1,
            "final",
            NodeExecutionId.for_node(run_id, revision.revision_hash, "final"),
            RunEventKind.AGENT_FAILED,
            AgentNodeRefusalRecord(
                AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED, detail
            ).encode(),
        ),
        None,
    )


def test_a_pre_attempt_refusal_is_bounded_by_its_sentence_not_read_as_a_code() -> None:
    """The refusal's sentence is any UTF-8 the ledger printed, and it is the
    field the bound applies to; only an attempt's payload is an ASCII code."""

    sentence = "claim branch 'x' does not match checkout branch 'Änderung/main'"

    api_limits(maximum_field_characters=len(sentence)).require_event_projection(
        _refused_before_any_attempt(sentence)
    )
    with pytest.raises(ApiLimitExceeded, match="detail"):
        api_limits(maximum_field_characters=len(sentence) - 1).require_event_projection(
            _refused_before_any_attempt(sentence)
        )


def test_json_body_limit_runs_before_fastapi_buffers_or_validates_the_body() -> None:
    mutations = RecordingMutationPorts()
    exact_body = b"{" + b" " * 15
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(exact_body)),
    )

    within_limit = client.post(
        "/atelier/api/v1/runs",
        content=exact_body,
        headers={"content-type": "application/json"},
    )
    oversized = client.post(
        "/atelier/api/v1/runs",
        content=exact_body + b" ",
        headers={"content-type": "application/json"},
    )

    assert_problem(within_limit, 422, "invalid-request")
    assert_problem(oversized, 422, "invalid-request")
    assert mutations.starts == []


@pytest.mark.parametrize(
    ("maximum_field_characters", "run_id"),
    [
        (12, "abcdefghijkl"),
        (10, "x"),
    ],
    ids=("public-reference-overflow", "maximum-cursor-only-overflow"),
)
def test_start_run_wire_identity_limits_reject_before_durable_work(
    maximum_field_characters: int, run_id: str
) -> None:
    mutations = RecordingMutationPorts()
    limited_fields = client_for(
        mutations,
        api_limits(maximum_field_characters=maximum_field_characters),
    )
    response = limited_fields.post(
        "/atelier/api/v1/runs",
        json={"run_id": run_id, "workflow_revision_hash": "0" * 64},
    )

    assert_problem(response, 422, "invalid-request")
    assert mutations.starts == []
    assert mutations.publications == []
    assert mutations.answers == []


def test_field_and_workflow_node_limits_reject_before_write() -> None:
    mutations = RecordingMutationPorts()

    node_limited = client_for(
        mutations,
        api_limits(maximum_workflow_nodes=1),
    )
    accepted = node_limited.post(
        "/atelier/api/v1/workflow-revisions",
        content=workflow_document(),
        headers={"content-type": "application/yaml"},
    )
    rejected = node_limited.post(
        "/atelier/api/v1/workflow-revisions",
        content=workflow_document(include_agent=True),
        headers={"content-type": "application/yaml"},
    )

    assert accepted.status_code == 201
    assert_problem(rejected, 422, "invalid-workflow-document")
    assert len(mutations.publications) == 1


def test_base64_and_decoded_payload_limits_reject_before_answer_write() -> None:
    mutations = RecordingMutationPorts()
    limits = api_limits(
        maximum_base64_characters=4,
        maximum_decoded_payload_bytes=2,
    )
    client = client_for(mutations, limits)
    path = (
        "/atelier/api/v1/runs/" + encode_public_run_reference(RunId("run")) + "/answers"
    )
    body = {
        "workflow_revision_hash": "0" * 64,
        "node_id": "wait",
        "expected_node_execution_id": "1" * 64,
        "actor": "operator",
        "answer_base64": encode_canonical_base64(b"12"),
    }

    exact = client.post(path, json=body)
    decoded_oversized = client.post(
        path,
        json={**body, "answer_base64": encode_canonical_base64(b"123")},
    )
    encoded_oversized = client.post(
        path,
        json={**body, "answer_base64": "AAAAA"},
    )

    assert exact.status_code == 404
    assert_problem(decoded_oversized, 422, "invalid-request")
    assert_problem(encoded_oversized, 422, "invalid-request")
    assert len(mutations.answers) == 1


@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_the_deployed_body_policy_admits_the_largest_answer_envelope() -> None:
    """The HTTP body cap owns the envelope, not the payload's base64 length."""
    mutations = RecordingMutationPorts()
    limits = deployed_api_limits()
    body = json.dumps(
        {
            "workflow_revision_hash": "0" * 64,
            "node_id": "n" * limits.maximum_field_characters,
            "expected_node_execution_id": "1" * 64,
            "actor": "operator",
            "answer_base64": encode_canonical_base64(
                b"1" + b"0" * (MAXIMUM_AGENT_OUTPUT_BYTES_V2 - 1)
            ),
        },
        separators=(",", ":"),
    ).encode("ascii")
    client = client_for(mutations, limits)
    path = (
        "/atelier/api/v1/runs/" + encode_public_run_reference(RunId("run")) + "/answers"
    )

    response = client.post(
        path,
        content=body,
        headers={"content-type": "application/json"},
    )

    assert len(body) > limits.maximum_base64_characters
    assert len(body) <= limits.maximum_request_body_bytes
    assert_problem(response, 404, "run-not-found")
    assert len(mutations.answers) == 1
