"""The duplex channel: one supervised child a conversation can speak with.

The subject is the seam, not a provider: a fake conversation stands in for the
wire format so that what is proven here is the relay -- who reads, who decides,
who writes, and which bound refuses -- and not one vendor's frames. One test
drives the standard ACP conversation instead, because a port only ever relayed
for a fake proves the fake.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters import agent_processes as process_module
from atelier2.adapters.agent_client_protocol import AgentClientProtocolConversation
from atelier2.adapters.agent_processes import AgentProcessSupervisor
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_permissions import (
    MINIMUM_PERMISSION_CALL_ORDINAL,
    PermissionCorrelationId,
    PermissionDecision,
    PermissionEffect,
    PermissionPolicyRevision,
    PermissionRequest,
    PermissionScope,
    PermissionScopeKind,
    PolicyPermissionDecider,
)
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    UnrecognisedProviderOutput,
)
from atelier2.contracts.agents import AgentExecutorRevision
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.process_endings import ProcessExitSignature
from atelier2.ports.agent_executions import (
    AgentProcessCompletion,
    AgentProcessInvocation,
    PermissionDecider,
    ProviderConversationBinding,
)
from atelier2.ports.provider_conversations import (
    ProviderCancellationCause,
    ProviderCancellationFrame,
    ProviderCancellationRequest,
    ProviderConversationAction,
    ProviderConversationBounds,
    ProviderConversationClosing,
    ProviderConversationComplete,
    ProviderConversationEnding,
    ProviderFilesystemAnswer,
    ProviderFilesystemEffect,
    ProviderFilesystemRefusal,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderFilesystemRequestId,
    ProviderSessionEvent,
    ProviderStandardInput,
    ProviderTerminalOutcome,
    ProviderTerminalReason,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
    process_invocation,
)

_WORKSPACE_SCOPE = PermissionScope(PermissionScopeKind.PATH_PREFIX, "/workspace")
_GRANTS_THE_WORKSPACE = PermissionPolicyRevision(
    frozenset({(PermissionEffect.WORKSPACE_READ, _WORKSPACE_SCOPE)})
)
_CONVERSING_REVISION = AgentExecutorRevision("fake-conversation/v1")
_ANOTHER_REVISION = AgentExecutorRevision("fake-conversation/v2")
_STANDARD_ACP_REVISION = AgentExecutorRevision("standard-acp/v1")
_ACP_SESSION = "01a06f4c-7326-79d0-9bde-ed08ee7e716c"

_PROVIDER_PRINTS_ONE_FRAME = r"""
import os, sys
os.write(1, sys.argv[1].encode())
"""

_PROVIDER_ASKS_THEN_REPEATS_THE_ANSWER = r"""
import os, sys
os.write(1, b'{"ask":"' + sys.argv[1].encode() + b'"}\n')
os.write(1, b'{"heard":' + sys.stdin.readline().strip().encode() + b'}\n')
"""

_PROVIDER_ASKS_AND_LEAVES = r"""
import os, sys
os.write(1, b'{"ask":"' + sys.argv[1].encode() + b'"}\n')
"""

_PROVIDER_WRITES_A_FILE_THEN_READS_IT_BACK = r"""
import os, sys
os.write(1, b'{"write":"' + sys.argv[1].encode() + b'","content":"' + sys.argv[2].encode() + b'"}\n')
sys.stdin.readline()
os.write(1, b'{"read":"' + sys.argv[1].encode() + b'"}\n')
os.write(1, b'{"heard":' + sys.stdin.readline().strip().encode() + b'}\n')
"""

_PROVIDER_READS_A_FILE_AND_REPEATS_THE_ANSWER = r"""
import os, sys
os.write(1, b'{"read":"' + sys.argv[1].encode() + b'"}\n')
os.write(1, b'{"heard":' + sys.stdin.readline().strip().encode() + b'}\n')
"""

_PROVIDER_REPEATS_WHATEVER_OPENED_IT = r"""
import os, sys
os.write(1, b'{"heard":' + sys.stdin.readline().strip().encode() + b'}\n')
"""

# End of file is no answer, and no reason to leave either: it keeps standing
# there, so only a stop of its own ends this child.
_PROVIDER_ASKS_AND_RECORDS_WHATEVER_ANSWER_ARRIVES = r"""
import os, sys, time
os.write(1, b'{"ask":"' + sys.argv[2].encode() + b'"}\n')
told = os.read(0, 4096)
if told:
    open(sys.argv[1], "wb").write(told)
time.sleep(60)
"""

_PROVIDER_ASKS_WITHOUT_EVER_READING = r"""
import os, sys, time
for _ in range(int(sys.argv[1])):
    os.write(1, b'{"ask":"' + sys.argv[2].encode() + b'"}\n')
    time.sleep(0.05)
time.sleep(60)
"""

_PROVIDER_SERVES_UNTIL_ITS_INPUT_ENDS = r"""
import os, sys
os.write(1, b'{"done":true}\n')
while sys.stdin.readline():
    pass
os.write(1, b'{"say":"bye"}\n')
"""

_PROVIDER_STOPS_ITSELF = r"""
import os, sys
os.write(1, b'{"stop":"' + sys.argv[1].encode() + b'"}\n')
"""

# It answers the handshake, the session and the prompt this client sends, and
# splits the one update in between so that the relay has to hand a partial
# frame to a conversation that owns its own reassembly.
_PROVIDER_SPEAKS_ACP_AND_SPLITS_ONE_UPDATE = r"""
import json, os, sys, time

def asked():
    return json.loads(sys.stdin.readline())

def answer(identifier, result):
    frame = json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result})
    os.write(1, frame.encode() + b"\n")

session, said = sys.argv[1], sys.argv[2]
answer(asked()["id"], {"protocolVersion": 1})
answer(asked()["id"], {"sessionId": session})
prompt = asked()
update = json.dumps({
    "jsonrpc": "2.0",
    "method": "session/update",
    "params": {
        "sessionId": session,
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": said},
        },
    },
}).encode()
os.write(1, update[:24])
time.sleep(0.2)
os.write(1, update[24:] + b"\n")
answer(prompt["id"], {"stopReason": "end_turn"})
"""

_PROVIDER_SPLITS_ONE_FRAME_AND_COALESCES_TWO = r"""
import os, time
os.write(1, b'{"say":"one')
time.sleep(0.2)
os.write(1, b'"}\n{"say":"two"}\n{"say":"three"}\n')
"""

_PROVIDER_WRITES_WHOLE_FRAMES = r"""
import os, sys
os.write(1, int(sys.argv[1]) * b'{"say":"line"}\n')
"""

_PROVIDER_WRITES_FRAMES_NOBODY_ANSWERS = r"""
import os, sys
os.write(1, int(sys.argv[1]) * b'{"ignore":"line"}\n')
"""

_PROVIDER_NEVER_ENDS_ITS_FRAME = r"""
import os, sys
os.write(1, b'{"say":"' + int(sys.argv[1]) * b'x')
"""

_PROVIDER_LEAVES_A_HALF_FRAME = r"""
import os
os.write(1, b'{"say":"whole"}\n{"say":"half')
"""

_PROVIDER_LEAVES_A_HALF_FRAME_AND_WAITS = r"""
import os, time
os.write(1, b'{"say":"whole"}\n{"say":"half')
time.sleep(60)
"""

# It keeps announcing itself so a test can wait for the exchange that carried
# the cancellation frame to the watchdog, ignores TERM so that what it was told
# is written down before it is killed, and keeps reading afterwards so a second
# stop frame would be written down too.
_PROVIDER_RECORDS_WHAT_IT_IS_TOLD = r"""
import os, select, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    os.write(1, b'{"say":"beat"}\n')
    ready, _writable, _failed = select.select([0], [], [], 0.05)
    if ready:
        told = os.read(0, 4096)
        if not told:
            break
        open(sys.argv[1], "ab").write(told)
time.sleep(60)
"""


# It names the cause its conversation will ask to be stopped for, ignores TERM
# so that what it was told is written down before it is killed, and keeps
# reading afterwards so a second stop frame would be written down too.
_PROVIDER_ASKS_TO_BE_STOPPED_AND_RECORDS_WHAT_IT_IS_TOLD = r"""
import os, select, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.write(1, b'{"cancel":"' + sys.argv[2].encode() + b'"}\n')
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    ready, _writable, _failed = select.select([0], [], [], 0.05)
    if ready:
        told = os.read(0, 4096)
        if not told:
            break
        open(sys.argv[1], "ab").write(told)
time.sleep(60)
"""


def _bounds(
    total: int = SCENARIO_PROVIDER_FRAME_BYTES,
    incomplete: int = 4_096,
    reply: int = 4_096,
    pending: int = 8_192,
) -> ProviderConversationBounds:
    return ProviderConversationBounds(total, incomplete, reply, 4_096, pending)


@dataclass
class _FakeFilesystemAccess:
    """The file access a binding is handed, answering from a scenario's own tree."""

    contents: dict[str, bytes] = field(default_factory=dict)
    requests: list[ProviderFilesystemRequest] = field(default_factory=list)

    def answer(self, request: ProviderFilesystemRequest) -> ProviderFilesystemReply:
        self.requests.append(request)
        named = str(request.path)
        if request.effect is ProviderFilesystemEffect.WRITE:
            self.contents[named] = request.content
            return ProviderFilesystemReply(
                request.request_id, ProviderFilesystemAnswer.ANSWERED
            )
        found = self.contents.get(named)
        if found is None:
            return ProviderFilesystemReply(
                request.request_id,
                ProviderFilesystemAnswer.REFUSED,
                refusal=ProviderFilesystemRefusal.FILE_NOT_FOUND,
            )
        return ProviderFilesystemReply(
            request.request_id, ProviderFilesystemAnswer.ANSWERED, found
        )


@dataclass
class _LineFramedConversation:
    """A provider that speaks one JSON object per line, and nothing else.

    `{"ask": scope}` is a permission question, `{"read": path}` and
    `{"write": path, "content": text}` are file requests, `{"stop": reason}` is
    the provider ending itself, `{"cancel": cause}` is this conversation asking
    for its own stop, `{"done": true}` is it saying it will send nothing more,
    `{"ignore": anything}` is a whole frame it has nothing to say about, every
    other line is a step, and whatever stands after the last newline is the
    half frame an ending has to account for.
    """

    attempt_id: AgentAttemptId
    bounds: ProviderConversationBounds = field(default_factory=_bounds)
    opening: bytes = b""
    cancellation_frame: bytes | None = None
    reply_padding: int = 0
    announces_acknowledgements: bool = False
    chunks: list[bytes] = field(default_factory=list)
    endings: list[ProviderConversationEnding] = field(default_factory=list)
    acknowledged: list[int] = field(default_factory=list)
    queued_input_bytes: int = 0
    questions: int = 0
    file_requests: int = 0
    refused_a_permission: bool = False
    provider_stop_reason: str = ""
    incomplete: bytes = b""

    @property
    def incomplete_frame_bytes(self) -> int:
        return len(self.incomplete)

    def open(self) -> tuple[ProviderConversationAction, ...]:
        if not self.opening:
            return ()
        return (self._queued(self.opening),)

    def input_written(
        self, written_bytes: int
    ) -> tuple[ProviderConversationAction, ...]:
        self.acknowledged.append(written_bytes)
        if not self.announces_acknowledgements:
            return ()
        return (
            ProviderSessionEvent(AssistantTurn(f'{{"acknowledged":{written_bytes}}}')),
        )

    def receive_output(self, chunk: bytes) -> tuple[ProviderConversationAction, ...]:
        self.chunks.append(chunk)
        self.incomplete += chunk
        actions: list[ProviderConversationAction] = []
        if self.cancellation_frame is not None:
            actions.append(ProviderCancellationFrame(self.cancellation_frame))
            self.cancellation_frame = None
        while b"\n" in self.incomplete:
            line, _newline, self.incomplete = self.incomplete.partition(b"\n")
            read = self._read(line.decode("utf-8"))
            if read is not None:
                actions.append(read)
        return tuple(actions)

    def answer_permission(self, decision: PermissionDecision) -> ProviderStandardInput:
        self.refused_a_permission = self.refused_a_permission or not decision.granted
        return self._spelled({"granted": decision.granted})

    def answer_filesystem(
        self, reply: ProviderFilesystemReply
    ) -> ProviderStandardInput:
        return self._spelled(
            {
                "file": reply.answer.value,
                "content": reply.content.decode("utf-8"),
                "request": reply.request_id.call_ordinal,
            }
        )

    def finish(self, ending: ProviderConversationEnding) -> ProviderConversationClosing:
        self.endings.append(ending)
        if not self.incomplete:
            return ProviderConversationClosing(self._outcome(ending))
        left = f"{ending.value}:{self.incomplete.decode('utf-8')}"
        return ProviderConversationClosing(
            self._outcome(ending),
            (ProviderSessionEvent(UnrecognisedProviderOutput(left)),),
        )

    def _spelled(self, answer: dict[str, object]) -> ProviderStandardInput:
        spelled = json.dumps(answer, separators=(",", ":")).encode("utf-8")
        return self._queued(spelled + self.reply_padding * b" " + b"\n")

    def _queued(self, data: bytes) -> ProviderStandardInput:
        self.queued_input_bytes += len(data)
        return ProviderStandardInput(data)

    def _outcome(self, ending: ProviderConversationEnding) -> ProviderTerminalOutcome:
        if self.provider_stop_reason:
            return ProviderTerminalOutcome(
                ProviderTerminalReason.CANCELLED_BY_PROVIDER, self.provider_stop_reason
            )
        if (
            self.refused_a_permission
            or ending is ProviderConversationEnding.CANCELLED_FOR_POLICY
        ):
            return ProviderTerminalOutcome(ProviderTerminalReason.POLICY_REFUSED)
        if ending is ProviderConversationEnding.CANCELLED_FOR_BUDGET:
            return ProviderTerminalOutcome(ProviderTerminalReason.BUDGET_EXHAUSTED)
        if ending is ProviderConversationEnding.CANCELLED_BY_OPERATOR:
            return ProviderTerminalOutcome(ProviderTerminalReason.CANCELLED_BY_OPERATOR)
        # A `TERMINATED` ending is supervision stopping this process for a
        # reason nobody told the conversation, and it never reaches a caller:
        # such an attempt has no completion to carry an outcome on.
        return ProviderTerminalOutcome(ProviderTerminalReason.ENDED)

    def _read(self, line: str) -> ProviderConversationAction | None:
        spoken = json.loads(line)
        if "ignore" in spoken:
            return None
        if "ask" in spoken:
            self.questions += 1
            return PermissionRequest(
                PermissionEffect.WORKSPACE_READ,
                PermissionScope(PermissionScopeKind.PATH_PREFIX, spoken["ask"]),
                PermissionCorrelationId.for_call(
                    self.attempt_id,
                    MINIMUM_PERMISSION_CALL_ORDINAL + self.questions - 1,
                ),
            )
        if "read" in spoken or "write" in spoken:
            self.file_requests += 1
            reading = "read" in spoken
            return ProviderFilesystemRequest(
                ProviderFilesystemEffect.READ
                if reading
                else ProviderFilesystemEffect.WRITE,
                Path(spoken["read"] if reading else spoken["write"]),
                ProviderFilesystemRequestId(self.file_requests),
                b"" if reading else spoken["content"].encode("utf-8"),
            )
        if "cancel" in spoken:
            return ProviderCancellationRequest(
                ProviderCancellationCause(spoken["cancel"])
            )
        if "done" in spoken:
            return ProviderConversationComplete()
        if "stop" in spoken:
            self.provider_stop_reason = spoken["stop"]
        return ProviderSessionEvent(AssistantTurn(line))


def _binding(
    conversation: _LineFramedConversation,
    revision: AgentExecutorRevision = _CONVERSING_REVISION,
    files: _FakeFilesystemAccess | None = None,
) -> ProviderConversationBinding:
    return ProviderConversationBinding(
        revision, conversation, files or _FakeFilesystemAccess()
    )


@dataclass
class _RecordingAuthority:
    """The bound policy, with a scenario's own delay or refusal in front of it."""

    policy: PermissionPolicyRevision = _GRANTS_THE_WORKSPACE
    before_answering: Callable[[], None] = lambda: None
    requests: list[PermissionRequest] = field(default_factory=list)

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        self.requests.append(request)
        self.before_answering()
        return PolicyPermissionDecider(self.policy).decide(request)


@dataclass
class _AuthorityNothingMayAsk:
    def decide(self, request: PermissionRequest) -> PermissionDecision:
        raise AssertionError(f"a print-mode invocation asked for {request.effect}")


@dataclass
class _ClaimedAttempt:
    execution: AgentAttemptExecution
    store: DbosAgentAttemptStore
    supervisor: AgentProcessSupervisor

    def launch(
        self, invocation: AgentProcessInvocation, permissions: PermissionDecider
    ) -> _Launch:
        completions: list[AgentProcessCompletion] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                completions.append(
                    self.supervisor.launch_and_wait(
                        self.execution, invocation, permissions
                    )
                )
            except BaseException as error:  # noqa: BLE001 - reported to the test
                failures.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        return _Launch(thread, completions, failures)

    def invocation(
        self,
        *arguments: str,
        conversation: ProviderConversationBinding | None = None,
    ) -> AgentProcessInvocation:
        return process_invocation(
            self.execution.attempt_id,
            (sys.executable, "-c", *arguments),
            Path.cwd(),
            conversation=conversation,
        )

    def finalize_after_failure(self) -> None:
        self.store.complete_known_failure(
            self.execution, ProcessExitSignature(0, b""), None
        )
        self.supervisor.finalize(self.execution)

    def request_cancellation(self) -> CancelAgentAttemptRequest:
        attempt = self.store.load(self.execution.attempt_id)
        command = CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            "cancel-conversation",
            attempt.state_version,
            AgentAttemptReplacement.NONE,
        )
        self.store.request_cancellation(command)
        return command

    def stop(
        self, cause: ProviderCancellationCause = ProviderCancellationCause.OPERATOR
    ) -> _Stopped:
        disposition, owner, generation = self.supervisor.cancel(
            self.store.load(self.execution.attempt_id), cause
        )
        return _Stopped(disposition, owner, generation)

    def attest_and_release(
        self, command: CancelAgentAttemptRequest, stopped: _Stopped
    ) -> None:
        terminal = self.store.attest_cancellation_cleanup(
            command, stopped.disposition, stopped.owner, stopped.generation
        )
        self.supervisor.release(terminal.attempt)

    def cancel_and_release(
        self, cause: ProviderCancellationCause = ProviderCancellationCause.OPERATOR
    ) -> AgentAttemptCancellationDisposition:
        command = self.request_cancellation()
        stopped = self.stop(cause)
        assert self.stop(cause) == stopped
        self.attest_and_release(command, stopped)
        return stopped.disposition


@dataclass(frozen=True)
class _Stopped:
    """What a stop attested: how the child went out, under whose supervision."""

    disposition: AgentAttemptCancellationDisposition
    owner: AgentProcessOwnerId
    generation: WatchdogGenerationId


@dataclass
class _Launch:
    thread: threading.Thread
    completions: list[AgentProcessCompletion]
    failures: list[BaseException]

    @property
    def completion(self) -> AgentProcessCompletion:
        self._joined()
        assert self.failures == []
        return self.completions[0]

    @property
    def failure(self) -> BaseException:
        self._joined()
        assert self.completions == []
        return self.failures[0]

    def _joined(self) -> None:
        self.thread.join(timeout=20)
        assert not self.thread.is_alive()


@contextmanager
def _claimed_attempt(tmp_path: Path, name: str) -> Iterator[_ClaimedAttempt]:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, name))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        yield _ClaimedAttempt(execution, store, supervisor)
    finally:
        runtime.close()


def _lose_the_first_acknowledged_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the watchdog answer one exchange, and lose that answer on the way back.

    The exchange that first reports written input is the one worth losing: its
    answer carried both what the child took and what it said, so a retry that
    counted either again would be visible.
    """

    request = process_module.AgentProcessSupervisor._request
    lost = threading.Event()

    def losing_one(
        endpoint: Path,
        payload: dict[str, object],
        *,
        timeout_seconds: float | None = 30,
        maximum_response_bytes: int,
    ) -> dict[str, object]:
        response = request(
            endpoint,
            payload,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )
        if (
            payload.get("operation") == "EXCHANGE"
            and response.get("accepted_input_bytes")
            and not lost.is_set()
        ):
            lost.set()
            raise TimeoutError("the exchange answer never arrived")
        return response

    monkeypatch.setattr(
        process_module.AgentProcessSupervisor, "_request", staticmethod(losing_one)
    )


def _wait_until(reached: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if reached():
            return
        time.sleep(0.01)
    raise AssertionError(f"the conversation never {what}")


def test_a_print_mode_invocation_names_no_conversation_and_is_never_asked(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/print-mode") as attempt:
        invocation = attempt.invocation(_PROVIDER_PRINTS_ONE_FRAME, '"done"')

        completion = attempt.launch(invocation, _AuthorityNothingMayAsk()).completion

        assert "duplex" not in process_module._launch_request(invocation)
        assert completion.standard_output == b'"done"'
        assert completion.session_events == ()
        assert completion.terminal_outcome is None
        attempt.finalize_after_failure()


def test_a_question_is_answered_by_the_bound_authority_and_reaches_the_child(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/asked") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        authority = _RecordingAuthority()
        invocation = attempt.invocation(
            _PROVIDER_ASKS_THEN_REPEATS_THE_ANSWER,
            _WORKSPACE_SCOPE.value,
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, authority).completion

        assert completion.standard_output == (
            b'{"ask":"/workspace"}\n{"heard":{"granted":true}}\n'
        )
        assert [request.scope for request in authority.requests] == [_WORKSPACE_SCOPE]
        assert completion.session_events == (
            AssistantTurn('{"heard":{"granted":true}}'),
        )
        assert conversation.endings == [ProviderConversationEnding.OUTPUT_ENDED]
        assert completion.terminal_outcome == ProviderTerminalOutcome(
            ProviderTerminalReason.ENDED
        )
        attempt.finalize_after_failure()


def test_a_refused_question_reaches_the_child_as_the_refusal_it_was(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/refused") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_ASKS_THEN_REPEATS_THE_ANSWER,
            "/elsewhere",
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        assert completion.standard_output.endswith(b'{"heard":{"granted":false}}\n')
        assert completion.terminal_outcome == ProviderTerminalOutcome(
            ProviderTerminalReason.POLICY_REFUSED
        )
        attempt.finalize_after_failure()


def test_split_and_coalesced_frames_reach_the_conversation_whole(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/framing") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_SPLITS_ONE_FRAME_AND_COALESCES_TWO,
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        assert completion.session_events == tuple(
            AssistantTurn(f'{{"say":"{spoken}"}}') for spoken in ("one", "two", "three")
        )
        # The child paused mid-frame, so the relay really did hand over a
        # partial one -- without that the reassembly above proves nothing.
        assert len(conversation.chunks) > 1
        assert b"".join(conversation.chunks) == completion.standard_output
        attempt.finalize_after_failure()


def test_the_standard_acp_conversation_speaks_with_a_real_child_through_the_relay(
    tmp_path: Path,
) -> None:
    """The seam is proven here with a fake, so the conversation this product
    ships is proven once against the relay itself: everything the relay asks of
    a conversation -- its bounds, its unfinished frame, the actions it
    publishes -- has to be there before a live attempt reads its first byte."""
    said = "half a frame, then the rest"
    with _claimed_attempt(tmp_path, "process/standard-acp") as attempt:
        conversation = AgentClientProtocolConversation(
            attempt.execution.attempt_id,
            "append one line to README.md",
            tmp_path,
            _bounds(),
            maximum_tool_calls=8,
        )
        invocation = attempt.invocation(
            _PROVIDER_SPEAKS_ACP_AND_SPLITS_ONE_UPDATE,
            _ACP_SESSION,
            said,
            conversation=ProviderConversationBinding(
                _STANDARD_ACP_REVISION, conversation, _FakeFilesystemAccess()
            ),
        )

        completion = attempt.launch(invocation, _AuthorityNothingMayAsk()).completion

        assert completion.session_events == (AssistantTurn(said),)
        assert completion.terminal_outcome == ProviderTerminalOutcome(
            ProviderTerminalReason.ENDED
        )
        attempt.finalize_after_failure()


def test_output_past_the_declared_total_ends_the_attempt_loudly(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/total-bound") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id, bounds=_bounds(total=64, incomplete=64)
        )
        invocation = attempt.invocation(
            _PROVIDER_WRITES_WHOLE_FRAMES,
            "64",
            conversation=_binding(conversation),
        )

        failure = attempt.launch(invocation, _RecordingAuthority()).failure

        assert isinstance(failure, RuntimeError)
        assert "output exceeds its declared bound" in str(failure)
        attempt.finalize_after_failure()


def test_a_frame_that_never_ends_refuses_at_the_declared_incomplete_bound(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/frame-bound") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id, bounds=_bounds(incomplete=64)
        )
        invocation = attempt.invocation(
            _PROVIDER_NEVER_ENDS_ITS_FRAME,
            "4096",
            conversation=_binding(conversation),
        )

        failure = attempt.launch(invocation, _RecordingAuthority()).failure

        assert isinstance(failure, RuntimeError)
        assert "frame exceeds its declared bound" in str(failure)
        attempt.finalize_after_failure()


def test_whole_frames_this_conversation_answers_nothing_to_end_no_attempt(
    tmp_path: Path,
) -> None:
    """The incomplete-frame bound refuses a sentence that never ends, never a
    stream of finished ones the conversation had nothing to say about."""
    with _claimed_attempt(tmp_path, "process/consumed-frames") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id, bounds=_bounds(incomplete=64)
        )
        invocation = attempt.invocation(
            _PROVIDER_WRITES_FRAMES_NOBODY_ANSWERS,
            "64",
            conversation=_binding(conversation),
        )

        launch = attempt.launch(invocation, _RecordingAuthority())

        assert launch.completion.terminal_outcome == ProviderTerminalOutcome(
            ProviderTerminalReason.ENDED
        )
        assert launch.completion.session_events == ()
        attempt.finalize_after_failure()


def test_an_answer_past_the_declared_reply_bound_is_never_written(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/reply-bound") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id,
            bounds=_bounds(reply=32),
            reply_padding=64,
        )
        invocation = attempt.invocation(
            _PROVIDER_ASKS_AND_LEAVES,
            _WORKSPACE_SCOPE.value,
            conversation=_binding(conversation),
        )

        failure = attempt.launch(invocation, _RecordingAuthority()).failure

        assert isinstance(failure, RuntimeError)
        assert "reply exceeds its declared bound" in str(failure)
        attempt.finalize_after_failure()


def test_an_authority_that_refuses_to_answer_stops_the_child_it_left_waiting(
    tmp_path: Path,
) -> None:
    told = tmp_path / "what-the-child-was-told"
    with _claimed_attempt(tmp_path, "process/failing-receipt") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)

        def refuse_to_keep_the_receipt() -> None:
            raise RuntimeError("the permission receipt could not be kept")

        invocation = attempt.invocation(
            _PROVIDER_ASKS_AND_RECORDS_WHATEVER_ANSWER_ARRIVES,
            str(told),
            _WORKSPACE_SCOPE.value,
            conversation=_binding(conversation),
        )

        failure = attempt.launch(
            invocation,
            _RecordingAuthority(before_answering=refuse_to_keep_the_receipt),
        ).failure

        assert isinstance(failure, RuntimeError)
        assert "receipt could not be kept" in str(failure)
        assert not told.exists()
        # The child was still waiting for the answer this failure means it
        # will never get. Finalization is accepted only by a watchdog holding
        # a reaped process, so this is where a child left alive would show.
        attempt.finalize_after_failure()


def test_input_a_child_never_reads_refuses_at_the_declared_pending_bound(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/pending-bound") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id,
            bounds=_bounds(pending=8_192),
            reply_padding=4_000,
        )
        invocation = attempt.invocation(
            _PROVIDER_ASKS_WITHOUT_EVER_READING,
            "40",
            _WORKSPACE_SCOPE.value,
            conversation=_binding(conversation),
        )

        failure = attempt.launch(invocation, _RecordingAuthority()).failure

        # Every answer is written into a pipe nobody drains: once it is full,
        # what the watchdog still holds unwritten is what this conversation
        # declared room for, not the far wider bound of the process seam.
        assert isinstance(failure, RuntimeError)
        assert "standard input exceeds its declared bound" in str(failure)
        attempt.finalize_after_failure()


def test_a_lost_exchange_answer_is_retried_without_delivering_a_byte_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _claimed_attempt(tmp_path, "process/lost-exchange") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        authority = _RecordingAuthority()
        _lose_the_first_acknowledged_exchange(monkeypatch)
        invocation = attempt.invocation(
            _PROVIDER_ASKS_THEN_REPEATS_THE_ANSWER,
            _WORKSPACE_SCOPE.value,
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, authority).completion

        assert completion.standard_output == (
            b'{"ask":"/workspace"}\n{"heard":{"granted":true}}\n'
        )
        # The lost answer carried both directions: the retry must not ask the
        # child twice, and must not read the same output into the conversation
        # twice either.
        assert len(authority.requests) == 1
        assert b"".join(conversation.chunks) == completion.standard_output
        attempt.finalize_after_failure()


def test_a_cancellation_does_not_wait_for_the_receipt_a_relay_is_blocked_on(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/blocked-receipt") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        asked = threading.Event()
        may_keep_the_receipt = threading.Event()

        def keep_the_receipt_when_allowed() -> None:
            asked.set()
            assert may_keep_the_receipt.wait(timeout=20)

        invocation = attempt.invocation(
            _PROVIDER_ASKS_THEN_REPEATS_THE_ANSWER,
            _WORKSPACE_SCOPE.value,
            conversation=_binding(conversation),
        )
        launch = attempt.launch(
            invocation,
            _RecordingAuthority(before_answering=keep_the_receipt_when_allowed),
        )
        assert asked.wait(timeout=10)
        command = attempt.request_cancellation()

        stopped = attempt.stop()

        # The relay is still inside the receipt this stop did not wait for,
        # and the child is already reaped.
        assert launch.thread.is_alive()
        assert stopped.disposition is (
            AgentAttemptCancellationDisposition.REAPED_AFTER_TERM
        )
        may_keep_the_receipt.set()
        assert launch.completion.terminal_outcome == ProviderTerminalOutcome(
            ProviderTerminalReason.CANCELLED_BY_OPERATOR
        )
        # The answer this receipt finally allowed was written into a child that
        # was already gone, and nothing told the conversation otherwise.
        assert conversation.acknowledged == []
        assert conversation.endings == [
            ProviderConversationEnding.CANCELLED_BY_OPERATOR
        ]
        attempt.attest_and_release(command, stopped)


def test_the_conversation_speaks_first_and_its_own_bytes_open_the_child(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/opening") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id, opening=b'{"hello":"initialize"}\n'
        )
        invocation = attempt.invocation(
            _PROVIDER_REPEATS_WHATEVER_OPENED_IT,
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        # The command carries no payload at all: the first bytes the child ever
        # read are the ones the conversation itself spelled.
        assert invocation.command.standard_input == b""
        assert completion.standard_output == b'{"heard":{"hello":"initialize"}}\n'
        attempt.finalize_after_failure()


def test_the_conversation_learns_which_of_its_bytes_the_child_really_has(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/acknowledged") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id, announces_acknowledgements=True
        )
        invocation = attempt.invocation(
            _PROVIDER_ASKS_THEN_REPEATS_THE_ANSWER,
            _WORKSPACE_SCOPE.value,
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        assert conversation.acknowledged[-1] == conversation.queued_input_bytes
        assert completion.session_events == (
            AssistantTurn(f'{{"acknowledged":{conversation.queued_input_bytes}}}'),
            AssistantTurn('{"heard":{"granted":true}}'),
        )
        attempt.finalize_after_failure()


def test_a_conversation_that_asks_to_be_stopped_stops_the_child_with_its_cause(
    tmp_path: Path,
) -> None:
    told = tmp_path / "what-the-child-was-told"
    stop_frame = b'{"stop":true}\n'
    with _claimed_attempt(tmp_path, "process/asked-to-stop") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id, cancellation_frame=stop_frame
        )
        invocation = attempt.invocation(
            _PROVIDER_ASKS_TO_BE_STOPPED_AND_RECORDS_WHAT_IT_IS_TOLD,
            str(told),
            ProviderCancellationCause.BUDGET.value,
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        # Nobody outside asked: the conversation reached its own ceiling, and
        # the stop it asked for is the one an operator would have got.
        assert told.read_bytes() == stop_frame
        assert conversation.endings == [ProviderConversationEnding.CANCELLED_FOR_BUDGET]
        assert completion.terminal_outcome == ProviderTerminalOutcome(
            ProviderTerminalReason.BUDGET_EXHAUSTED
        )
        attempt.finalize_after_failure()


def test_a_completed_conversation_lets_its_child_leave_on_end_of_file(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/completed") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_SERVES_UNTIL_ITS_INPUT_ENDS,
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        # A server that waits for more input leaves without a signal only if
        # somebody closes that input: nothing here was terminated.
        assert completion.return_code == 0
        assert completion.session_events == (AssistantTurn('{"say":"bye"}'),)
        assert conversation.endings == [ProviderConversationEnding.OUTPUT_ENDED]
        attempt.finalize_after_failure()


def test_a_half_frame_is_kept_as_evidence_of_output_that_simply_ran_out(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/half-frame-eof") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_LEAVES_A_HALF_FRAME, conversation=_binding(conversation)
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        assert conversation.endings == [ProviderConversationEnding.OUTPUT_ENDED]
        assert completion.session_events == (
            AssistantTurn('{"say":"whole"}'),
            UnrecognisedProviderOutput('output-ended:{"say":"half'),
        )
        attempt.finalize_after_failure()


def test_a_half_frame_left_by_a_stopped_child_ends_the_conversation_as_cancelled(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/half-frame-stopped") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_LEAVES_A_HALF_FRAME_AND_WAITS,
            conversation=_binding(conversation),
        )

        launch = attempt.launch(invocation, _RecordingAuthority())
        _wait_until(lambda: bool(conversation.chunks), "read the child's half frame")
        disposition = attempt.cancel_and_release()

        assert conversation.endings == [
            ProviderConversationEnding.CANCELLED_BY_OPERATOR
        ]
        assert launch.completion.session_events == (
            AssistantTurn('{"say":"whole"}'),
            UnrecognisedProviderOutput('cancelled-by-operator:{"say":"half'),
        )
        assert disposition is AgentAttemptCancellationDisposition.REAPED_AFTER_TERM


def test_cancellation_writes_the_published_frame_once_and_signals_beside_it(
    tmp_path: Path,
) -> None:
    told = tmp_path / "what-the-child-was-told"
    stop_frame = b'{"stop":true}\n'
    with _claimed_attempt(tmp_path, "process/cancel-frame") as attempt:
        conversation = _LineFramedConversation(
            attempt.execution.attempt_id, cancellation_frame=stop_frame
        )
        invocation = attempt.invocation(
            _PROVIDER_RECORDS_WHAT_IT_IS_TOLD,
            str(told),
            conversation=_binding(conversation),
        )

        launch = attempt.launch(invocation, _RecordingAuthority())
        # The exchange after the first one carried the frame to the watchdog.
        _wait_until(lambda: len(conversation.chunks) > 1, "published its stop frame")
        disposition = attempt.cancel_and_release()

        assert conversation.endings == [
            ProviderConversationEnding.CANCELLED_BY_OPERATOR
        ]
        assert launch.completion.session_events[0] == AssistantTurn('{"say":"beat"}')
        assert told.read_bytes() == stop_frame
        assert disposition is AgentAttemptCancellationDisposition.REAPED_AFTER_KILL


def test_a_file_request_is_answered_by_the_bound_access_and_reaches_the_child(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/files") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        files = _FakeFilesystemAccess()
        invocation = attempt.invocation(
            _PROVIDER_WRITES_A_FILE_THEN_READS_IT_BACK,
            "notes.txt",
            "kept",
            conversation=_binding(conversation, files=files),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        assert [
            (request.effect, str(request.path), request.content)
            for request in files.requests
        ] == [
            (ProviderFilesystemEffect.WRITE, "notes.txt", b"kept"),
            (ProviderFilesystemEffect.READ, "notes.txt", b""),
        ]
        assert files.contents == {"notes.txt": b"kept"}
        assert completion.standard_output.endswith(
            b'{"heard":{"file":"answered","content":"kept","request":2}}\n'
        )
        attempt.finalize_after_failure()


def test_a_file_request_the_access_refuses_reaches_the_child_as_that_refusal(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/files-refused") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_READS_A_FILE_AND_REPEATS_THE_ANSWER,
            "secrets.txt",
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        assert completion.standard_output.endswith(
            b'{"heard":{"file":"refused","content":"","request":1}}\n'
        )
        attempt.finalize_after_failure()


def test_a_provider_that_stopped_itself_keeps_its_own_stop_reason(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/provider-stop") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_STOPS_ITSELF,
            "context window",
            conversation=_binding(conversation),
        )

        completion = attempt.launch(invocation, _RecordingAuthority()).completion

        assert completion.terminal_outcome == ProviderTerminalOutcome(
            ProviderTerminalReason.CANCELLED_BY_PROVIDER, "context window"
        )
        attempt.finalize_after_failure()


@pytest.mark.parametrize(
    ("cause", "reason"),
    (
        (
            ProviderCancellationCause.OPERATOR,
            ProviderTerminalReason.CANCELLED_BY_OPERATOR,
        ),
        (ProviderCancellationCause.BUDGET, ProviderTerminalReason.BUDGET_EXHAUSTED),
        (ProviderCancellationCause.POLICY, ProviderTerminalReason.POLICY_REFUSED),
    ),
)
def test_the_cause_a_cancellation_named_reaches_the_terminal_outcome(
    tmp_path: Path,
    cause: ProviderCancellationCause,
    reason: ProviderTerminalReason,
) -> None:
    with _claimed_attempt(tmp_path, f"process/cancel-{cause.value}") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        invocation = attempt.invocation(
            _PROVIDER_LEAVES_A_HALF_FRAME_AND_WAITS,
            conversation=_binding(conversation),
        )

        launch = attempt.launch(invocation, _RecordingAuthority())
        _wait_until(lambda: bool(conversation.chunks), "read the child's half frame")
        attempt.cancel_and_release(cause)

        assert conversation.endings == [
            ProviderConversationEnding.of_cancellation(cause)
        ]
        assert launch.completion.terminal_outcome == ProviderTerminalOutcome(reason)


def test_a_retry_carrying_another_conversation_revision_is_refused(
    tmp_path: Path,
) -> None:
    with _claimed_attempt(tmp_path, "process/other-revision") as attempt:
        conversation = _LineFramedConversation(attempt.execution.attempt_id)
        launched = attempt.invocation(
            _PROVIDER_LEAVES_A_HALF_FRAME, conversation=_binding(conversation)
        )
        completion = attempt.launch(launched, _RecordingAuthority()).completion

        with pytest.raises(RuntimeError, match="invocation changed"):
            attempt.supervisor.launch_and_wait(
                attempt.execution,
                attempt.invocation(
                    _PROVIDER_LEAVES_A_HALF_FRAME,
                    conversation=_binding(
                        _LineFramedConversation(attempt.execution.attempt_id),
                        _ANOTHER_REVISION,
                    ),
                ),
                _RecordingAuthority(),
            )

        assert attempt.launch(launched, _RecordingAuthority()).completion is completion
        assert (
            attempt.store.load(attempt.execution.attempt_id).process_phase
            is AgentAttemptProcessPhase.PROCESS_OBSERVED
        )
        attempt.finalize_after_failure()
