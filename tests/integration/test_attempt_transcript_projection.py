"""GET /runs/{ref}/nodes/{node_id} serves a stored attempt transcript.

Calling the projection constructor is not the proof: a transcript handed to
`node_detail_resource` never travelled the store. These tests write the
artifact the way production writes it — the attempt names
`transcript_artifact_hash`, the bytes sit in `artifacts` — then read the node
through the HTTP entry point.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.artifact_store import keep_artifact, read_stored_artifact
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import agent_attempts
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.api.wire.resources import (
    TranscriptBeforeMomentsOrigin,
    TranscriptBeforeMomentsResource,
    TranscriptRecordedMomentResource,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    ProviderTerminalRefusal,
    ToolCalled,
    ToolReturned,
    TranscriptMomentOrigin,
    UnrecognisedProviderOutput,
    Usage,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.contracts.runs import RunId
from atelier2.contracts.secret_redaction import REDACTION_MARKER
from atelier2.contracts.when import RecordedAt
from atelier2.ports.agent_attempts import AgentAttemptFailed, AgentAttemptSucceeded
from atelier2.ports.agent_executions import AgentExecutionFailure
from atelier2.ports.run_queries import NodeDetailFound
from tests.domain.test_agent_transcripts import planted_credential
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    RecordingAgentExecutorV2,
    agent_attempt_execution,
    answering,
    launching,
    runtime_workspace_owner,
    workspace_files_nobody_opens,
)
from tests.scenarios.api import durable_api_client, durable_queries

NODE_ID = "build"
TRANSCRIPT_RECORDED_AT = RecordedAt("2026-08-29T12:00:00Z")

_RECORDED_MOMENT = TranscriptRecordedMomentResource(
    recorded_at=TRANSCRIPT_RECORDED_AT.value,
    origin=TranscriptMomentOrigin.RECORDED,
).model_dump(mode="json")
_V1_BEFORE_MOMENTS = TranscriptBeforeMomentsResource(
    origin=TranscriptBeforeMomentsOrigin.V1
).model_dump(mode="json")


def _canary() -> str:
    return planted_credential("sk-ant", "-plantedcanarysecret0123456789")


_SUCCEEDED_STEPS = (
    ToolCalled("Read", '{"file_path":"/etc/hosts"}'),
    ToolReturned("Read", "127.0.0.1 localhost"),
    AssistantTurn("The host file names localhost."),
    Usage(1_200, 48),
)
_SUCCEEDED_EVENTS: list[dict[str, Any]] = [
    {
        "event": "tool-called",
        "name": "Read",
        "arguments": '{"file_path":"/etc/hosts"}',
        "redacted": False,
        "moment": _RECORDED_MOMENT,
    },
    {
        "event": "tool-returned",
        "name": "Read",
        "result": "127.0.0.1 localhost",
        "redacted": False,
        "moment": _RECORDED_MOMENT,
    },
    {
        "event": "assistant-turn",
        "text": "The host file names localhost.",
        "redacted": False,
        "moment": _RECORDED_MOMENT,
    },
    {
        "event": "usage",
        "input_tokens": 1_200,
        "output_tokens": 48,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "moment": _RECORDED_MOMENT,
    },
]


@dataclass(frozen=True)
class TranscriptNodeHttp:
    """One finished attempt, readable through the node-detail door."""

    client: TestClient
    runtime: DbosRuntime
    run_id: RunId
    public_ref: str
    node_id: str = NODE_ID

    def get(self) -> Response:
        return self.client.get(
            f"{API_PREFIX}/runs/{self.public_ref}/nodes/{self.node_id}"
        )

    def stored_transcript_bytes(self) -> bytes | None:
        with self.runtime.engine.connect() as connection:
            named = connection.scalar(
                sa.select(agent_attempts.c.transcript_artifact_hash)
            )
            if named is None:
                return None
            artifact = read_stored_artifact(connection, ArtifactHash(str(named)))
            assert artifact is not None
            return artifact.content

    def queried_detail(self) -> NodeDetailFound:
        found = durable_queries(self.runtime.engine).get_node_detail(
            self.run_id, self.node_id
        )
        assert isinstance(found, NodeDetailFound), found
        return found

    def replace_stored_transcript_with(self, transcript: AttemptTranscript) -> None:
        """Make this completed attempt point at a v1 artifact a past writer kept."""

        artifact = Artifact(transcript.document)
        with self.runtime.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(sa.text("DROP TRIGGER agent_attempts_state_transition"))
            keep_artifact(connection, artifact)
            connection.execute(
                agent_attempts.update().values(
                    transcript_artifact_hash=artifact.artifact_hash.value
                )
            )
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


ArrangeTranscriptNode = Callable[
    [AgentExecutionResult | AgentExecutionFailure], TranscriptNodeHttp
]


@pytest.fixture
def arranged_transcript_node(tmp_path: Path) -> Iterator[ArrangeTranscriptNode]:
    """One durable runtime, one attempt, written the production way."""

    runtimes: list[DbosRuntime] = []

    def arrange(
        verdict: AgentExecutionResult | AgentExecutionFailure,
    ) -> TranscriptNodeHttp:
        runtime = attempt_runtime(tmp_path)
        runtime.initialize_storage()
        runtimes.append(runtime)
        request = attempt_request(runtime, "transcript-http")
        outcome = execute_agent_attempt(
            agent_attempt_execution(request),
            RecordingAgentExecutorV2(
                command=launching(sys.executable, "-c", "pass"),
                decoder=answering(verdict),
            ),
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
            clock=lambda: TRANSCRIPT_RECORDED_AT,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )
        if isinstance(verdict, AgentExecutionFailure):
            assert isinstance(outcome, AgentAttemptFailed), outcome
        else:
            assert isinstance(outcome, AgentAttemptSucceeded), outcome
        return TranscriptNodeHttp(
            client=durable_api_client(runtime),
            runtime=runtime,
            run_id=request.run_id,
            public_ref=encode_public_run_reference(request.run_id),
        )

    try:
        yield arrange
    finally:
        for runtime in reversed(runtimes):
            runtime.close()


def _success(transcript: AttemptTranscript | None = None) -> AgentExecutionResult:
    return AgentExecutionResult(b'"done"', transcript)


def _failure(transcript: AttemptTranscript) -> AgentExecutionFailure:
    return AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY, transcript
    )


def _canary_in_tool_call_arguments(canary: str) -> AgentExecutionResult:
    return _success(
        AttemptTranscript.of([ToolCalled("Bash", f'{{"command":"{canary}"}}')])
    )


def _canary_in_tool_result(canary: str) -> AgentExecutionResult:
    return _success(
        AttemptTranscript.of([ToolReturned("Bash", f"ANTHROPIC_API_KEY={canary}\n")])
    )


def _canary_in_failed_stdout(canary: str) -> AgentExecutionFailure:
    return _failure(
        AttemptTranscript.of(
            [UnrecognisedProviderOutput(f"fatal: {canary} was rejected")]
        )
    )


@pytest.mark.parametrize(
    ("verdict", "expected_events"),
    [
        pytest.param(
            _success(AttemptTranscript.of(_SUCCEEDED_STEPS)),
            _SUCCEEDED_EVENTS,
            id="succeeded attempt with tool calls, results, assistant turns and usage",
        ),
        pytest.param(
            _failure(
                AttemptTranscript.of(
                    [UnrecognisedProviderOutput(f"fatal: token {_canary()} rejected")]
                )
            ),
            [
                {
                    "event": "unrecognised-provider-output",
                    "text": f"fatal: token {REDACTION_MARKER} rejected",
                    "redacted": True,
                    "moment": _RECORDED_MOMENT,
                }
            ],
            id="failed attempt with redacted stdout",
        ),
        pytest.param(
            _failure(
                AttemptTranscript.of(
                    [
                        ProviderTerminalRefusal(
                            "rate_limit_error", "429", f"blocked by {_canary()}"
                        )
                    ]
                )
            ),
            [
                {
                    "event": "provider-terminal-refusal",
                    "terminal_reason": "rate_limit_error",
                    "api_error_status": "429",
                    "text": f"blocked by {REDACTION_MARKER}",
                    "redacted": True,
                    "moment": _RECORDED_MOMENT,
                }
            ],
            id="failed attempt with a provider-refused step",
        ),
        pytest.param(
            _success(),
            None,
            id="attempt without a transcript",
        ),
    ],
)
def test_get_node_detail_projects_the_stored_transcript(
    arranged_transcript_node: ArrangeTranscriptNode,
    verdict: AgentExecutionResult | AgentExecutionFailure,
    expected_events: list[dict[str, Any]] | None,
) -> None:
    node = arranged_transcript_node(verdict)
    stored = node.stored_transcript_bytes()
    response = node.get()

    assert response.status_code == 200
    body = response.json()
    if expected_events is None:
        assert stored is None
        assert "transcript" not in body
        assert b'"transcript":null' not in response.content
        return
    assert stored is not None
    assert body["transcript"] == {"events": expected_events}
    assert "kind" not in body["transcript"]
    assert "document" not in body["transcript"]


def test_get_node_detail_names_a_v1_transcript_event_as_before_moments(
    arranged_transcript_node: ArrangeTranscriptNode,
) -> None:
    """The old document reaches the HTTP reader as a known legacy state."""

    node = arranged_transcript_node(_success(AttemptTranscript.of(_SUCCEEDED_STEPS)))
    node.replace_stored_transcript_with(
        AttemptTranscript.of([AssistantTurn("A v1 transcript predates moments.")])
    )

    response = node.get()

    assert response.status_code == 200
    assert response.json()["transcript"]["events"] == [
        {
            "event": "assistant-turn",
            "text": "A v1 transcript predates moments.",
            "redacted": False,
            "moment": _V1_BEFORE_MOMENTS,
        }
    ]


@pytest.mark.parametrize(
    "verdict_for",
    [
        pytest.param(_canary_in_tool_call_arguments, id="in tool-call arguments"),
        pytest.param(_canary_in_tool_result, id="in a tool result"),
        pytest.param(_canary_in_failed_stdout, id="in failed stdout"),
    ],
)
def test_a_canary_never_appears_on_the_http_body(
    arranged_transcript_node: ArrangeTranscriptNode,
    verdict_for: Callable[[str], AgentExecutionResult | AgentExecutionFailure],
) -> None:
    """The canary is planted, then hunted through artifact, query, and HTTP.

    JSON equality on the events would miss a leak in another field or in a
    document the resource was never meant to carry. The raw body is the
    surface an operator's client actually receives.
    """

    canary = _canary()
    node = arranged_transcript_node(verdict_for(canary))
    stored = node.stored_transcript_bytes()
    queried = node.queried_detail()
    response = node.get()

    assert stored is not None
    assert canary.encode() not in stored
    assert queried.detail.transcript is not None
    assert canary.encode() not in queried.detail.transcript.document
    assert canary not in str(queried.detail.transcript)
    assert response.status_code == 200
    assert canary.encode() not in response.content
    assert REDACTION_MARKER.encode() in response.content
    assert all(
        event["redacted"] is True for event in response.json()["transcript"]["events"]
    )
