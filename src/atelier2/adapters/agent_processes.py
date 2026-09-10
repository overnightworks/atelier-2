from __future__ import annotations

import base64
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import assert_never

from atelier2.adapters.agent_process_watchdog import (
    CONTROL_FRAME_TIMEOUT_SECONDS,
    EXCHANGE_HOLD_SECONDS,
    MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES,
    MAXIMUM_AGENT_EXCHANGE_RESPONSE_BYTES,
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
    encode_control_frame,
    maximum_agent_wait_response_bytes,
)
from atelier2.adapters.bwrap_sandbox import sandbox_frame
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptState,
    AgentProcessOwnerId,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_permissions import PermissionRequest
from atelier2.contracts.agent_transcripts import TranscriptEvent
from atelier2.contracts.agents import AgentExecutorRevision
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import AgentAttemptStore
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessCompletion,
    AgentProcessInvocation,
    AgentProcessOwnerNotLocal,
    AgentSession,
    PermissionDecider,
    ProviderConversationBinding,
)
from atelier2.ports.provider_conversations import (
    ProviderCancellationCause,
    ProviderCancellationFrame,
    ProviderCancellationRequest,
    ProviderConversationAction,
    ProviderConversationComplete,
    ProviderConversationEnding,
    ProviderFilesystemRequest,
    ProviderSessionEvent,
    ProviderStandardInput,
    ProviderTerminalOutcome,
)

_ENDING_OF_EXCHANGE_ARM: dict[str, ProviderConversationEnding] = {
    "COMPLETED": ProviderConversationEnding.OUTPUT_ENDED,
    **{
        cause.value: ProviderConversationEnding.of_cancellation(cause)
        for cause in ProviderCancellationCause
    },
}
"""How the watchdog's own ending vocabulary reads to a conversation."""

MAXIMUM_AGENT_CONTROL_REQUEST_ATTEMPTS = 2


@dataclass
class _OwnedWatchdog:
    owner: AgentProcessOwnerId
    generation: WatchdogGenerationId
    endpoint: Path
    cgroup: Path
    process: subprocess.Popen[bytes]
    owner_pipe: int
    launched: bool = False
    wait_completed: threading.Event = field(default_factory=threading.Event)
    condition: threading.Condition = field(default_factory=threading.Condition)
    launch_frame: bytes | None = None
    conversation_revision: AgentExecutorRevision | None = None
    completion: AgentProcessCompletion | None = None
    failure: str | None = None
    owner_not_local_failure: bool = False
    closed: bool = False
    finalizing: bool = False
    finalized: bool = False


def _leads_attempt(
    owned: _OwnedWatchdog, frame: bytes, revision: AgentExecutorRevision | None
) -> bool:
    with owned.condition:
        if owned.closed or owned.finalized:
            raise AgentProcessOwnerNotLocal
        if owned.launch_frame is None:
            owned.launch_frame = frame
            owned.conversation_revision = revision
            return True
        if owned.launch_frame != frame or owned.conversation_revision != revision:
            # A conversation is live state this armed session cannot share: two
            # revisions speaking into one child read each other's frames as their own.
            raise RuntimeError("local watchdog invocation changed")
        return False


@dataclass(frozen=True)
class _ConversationEvidence:
    """What a conversation left for the completion: its steps and its outcome."""

    steps: tuple[TranscriptEvent, ...] = ()
    outcome: ProviderTerminalOutcome | None = None


_NOTHING_WAS_SAID = _ConversationEvidence()
"""What a print-mode invocation leaves: no conversation ever ran."""


class _ConversationRelay:
    """One conversation's bounded state on the side that holds the process.

    It owns everything the conversation is not allowed to own: how many bytes
    have crossed in each direction, what is still waiting to be written, which
    cancellation frame the watchdog has been given, and the steps published so
    far. It is also the one place a question or a file request is carried to
    the authority that answers it, so the conversation reaches neither. Every
    bound the executor declared is checked here, because this is the side that
    allocates the buffers -- a conversation that never finishes a frame,
    answers past its reply bound or floods standard input ends the attempt
    loudly instead of being trusted.
    """

    def __init__(
        self, conversation: ProviderConversationBinding, permissions: PermissionDecider
    ) -> None:
        self._driver = conversation.driver
        self._files = conversation.files
        self._bounds = conversation.driver.bounds
        self._permissions = permissions
        self._pending_input = b""
        self._written_input_bytes = 0
        self._delivered_output_bytes = 0
        self._unpublished_cancellation: bytes | None = None
        self._requested_cancellation: ProviderCancellationCause | None = None
        self._input_complete = False
        self._steps: list[TranscriptEvent] = []

    def open(self) -> None:
        """Let the conversation say the first thing, before anything is read."""

        self._apply(self._driver.open())

    def exchange_request(self) -> dict[str, object]:
        """What this relay hands over next, addressed by cumulative byte counts.

        Both directions are counted, so the exchange the control channel had to
        retry delivers nothing twice: the watchdog skips input it already took,
        and output it already sent simply arrives again. Input counts as gone
        only once the child's own pipe has it, which is what keeps everything
        still buffered anywhere inside the bound this conversation declared.
        """

        return {
            "cancellation_frame": base64.b64encode(
                self._unpublished_cancellation or b""
            ).decode("ascii"),
            "close_input": self._input_complete,
            "delivered_output_bytes": self._delivered_output_bytes,
            "operation": "EXCHANGE",
            "standard_input": base64.b64encode(self._pending_input).decode("ascii"),
            "standard_input_offset": self._written_input_bytes,
        }

    def exchanged(
        self, response: dict[str, object]
    ) -> ProviderConversationEnding | None:
        """Read one exchange, and say how the process ended if it has."""

        written_bytes = response.get("accepted_input_bytes")
        output_value = response.get("standard_output")
        ending = response.get("ending")
        if (
            type(written_bytes) is not int
            or type(output_value) is not str
            or type(ending) is not str
            or not self._written_input_bytes
            <= written_bytes
            <= self._written_input_bytes + len(self._pending_input)
        ):
            raise RuntimeError("watchdog conversation exchange is malformed")
        try:
            chunk = base64.b64decode(output_value, validate=True)
        except ValueError as error:
            raise RuntimeError("watchdog conversation exchange is malformed") from error
        newly_written = written_bytes - self._written_input_bytes
        self._pending_input = self._pending_input[newly_written:]
        self._written_input_bytes = written_bytes
        self._unpublished_cancellation = None
        if newly_written:
            self._apply(self._driver.input_written(written_bytes))
        if chunk:
            self._receive(chunk)
            return None
        if not ending:
            return None
        return _ENDING_OF_EXCHANGE_ARM.get(
            ending, ProviderConversationEnding.TERMINATED
        )

    def cancellation_to_request(self) -> ProviderCancellationCause | None:
        """The stop this conversation asked for, once its frame is over there.

        Held back while a freshly published frame is still on this side: the
        watchdog writes what it was given and signals in the same breath, so a
        stop asked for before its own frame arrived would be a signal without
        the sentence that was meant to precede it.
        """

        if self._unpublished_cancellation is not None:
            return None
        requested = self._requested_cancellation
        self._requested_cancellation = None
        return requested

    def ended(self, ending: ProviderConversationEnding) -> _ConversationEvidence:
        """Let the conversation close its own story, and hand back what it left."""

        closing = self._driver.finish(ending)
        self._steps.extend(event.step for event in closing.steps)
        return _ConversationEvidence(tuple(self._steps), closing.outcome)

    def _receive(self, chunk: bytes) -> None:
        self._delivered_output_bytes += len(chunk)
        if self._delivered_output_bytes > self._bounds.maximum_total_output_bytes:
            raise RuntimeError("conversation output exceeds its declared bound")
        actions = self._driver.receive_output(chunk)
        if self._driver.incomplete_frame_bytes > (
            self._bounds.maximum_incomplete_frame_bytes
        ):
            raise RuntimeError("conversation frame exceeds its declared bound")
        self._apply(actions)

    def _apply(self, actions: tuple[ProviderConversationAction, ...]) -> None:
        for action in actions:
            match action:
                case ProviderStandardInput(data):
                    self._queue_input(data)
                case ProviderCancellationFrame(data):
                    if len(data) > self._bounds.maximum_cancel_bytes:
                        raise RuntimeError(
                            "conversation cancellation frame exceeds its declared bound"
                        )
                    self._unpublished_cancellation = data
                case ProviderCancellationRequest(cause):
                    self._requested_cancellation = cause
                case ProviderConversationComplete():
                    self._input_complete = True
                case PermissionRequest() as request:
                    decision = self._permissions.decide(request)
                    self._queue_input(self._driver.answer_permission(decision).data)
                case ProviderFilesystemRequest() as request:
                    reply = self._files.answer(request)
                    self._queue_input(self._driver.answer_filesystem(reply).data)
                case ProviderSessionEvent(step):
                    self._steps.append(step)
                case _ as unreachable:
                    assert_never(unreachable)

    def _queue_input(self, data: bytes) -> None:
        if len(data) > self._bounds.maximum_reply_bytes:
            raise RuntimeError("conversation reply exceeds its declared bound")
        if len(self._pending_input) + len(data) > (
            self._bounds.maximum_pending_input_bytes
        ):
            raise RuntimeError("conversation standard input exceeds its declared bound")
        self._pending_input += data


class AgentProcessSupervisor(AgentSession):
    """Serve's own `AgentSession`: one provider child per attempt, watched.

    Live signal authority; durable state contains no PID or invocation.
    """

    def __init__(
        self,
        store: AgentAttemptStore,
        control_root: Path,
        cgroup_root: Path,
        *,
        grace_seconds: float,
        ready_timeout_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._control_root = control_root.resolve()
        self._cgroup_root = cgroup_root.resolve()
        self._grace_seconds = grace_seconds
        self._ready_timeout_seconds = ready_timeout_seconds
        self._owner = AgentProcessOwnerId(uuid.uuid4().hex)
        self._registry_lock = threading.Lock()
        self._attempt_locks: dict[AgentAttemptId, threading.Lock] = {}
        self._owned: dict[AgentAttemptId, _OwnedWatchdog | None] = {}
        self._closed = False
        if grace_seconds <= 0 or ready_timeout_seconds <= 0:
            raise ValueError("agent process bounds must be positive")

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        with self._registry_lock:
            if self._closed:
                raise RuntimeError("agent process supervisor is closed")
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            existing = self._owned.get(execution.attempt_id)
            if existing is not None:
                return self._store.load(execution.attempt_id)
            durable = self._store.load(execution.attempt_id)
            if durable.state is not AgentAttemptState.PREPARED:
                return durable
            generation = WatchdogGenerationId(uuid.uuid4().hex)
            endpoint = self._control_root / f"{execution.attempt_id.value}.sock"
            cgroup = self._cgroup_root / (
                "atelier2-"
                + execution.attempt_id.value[:16]
                + "-"
                + generation.value[:8]
            )
            self._control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._control_root, 0o700)
            cgroup.mkdir(mode=0o700)
            try:
                read_pipe, write_pipe = os.pipe()
                try:
                    control_directory = os.open(
                        self._control_root, os.O_RDONLY | os.O_DIRECTORY
                    )
                    try:
                        process = subprocess.Popen(
                            (
                                sys.executable,
                                "-m",
                                "atelier2.adapters.agent_process_watchdog",
                                "--endpoint",
                                str(
                                    Path("/proc/self/fd")
                                    / str(control_directory)
                                    / endpoint.name
                                ),
                                "--cgroup",
                                str(cgroup),
                                "--owner-pipe",
                                str(read_pipe),
                                "--grace",
                                str(self._grace_seconds),
                            ),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            pass_fds=(read_pipe, control_directory),
                        )
                    finally:
                        os.close(control_directory)
                except BaseException:
                    os.close(read_pipe)
                    os.close(write_pipe)
                    raise
            except BaseException:
                try:
                    cgroup.rmdir()
                except OSError:
                    pass
                raise
            os.close(read_pipe)
            owned = _OwnedWatchdog(
                self._owner, generation, endpoint, cgroup, process, write_pipe
            )
            try:
                self._wait_ready(owned)
                with self._registry_lock:
                    if self._closed:
                        publish = False
                    else:
                        durable = self._store.bind_watchdog(
                            execution, self._owner, generation
                        )
                        self._owned[execution.attempt_id] = owned
                        publish = True
            except BaseException:
                os.close(write_pipe)
                process.kill()
                process.wait()
                _close_watchdog_pipes(process)
                endpoint.unlink(missing_ok=True)
                try:
                    cgroup.rmdir()
                except OSError:
                    pass
                raise
            if not publish:
                self._close_owned(owned)
                raise RuntimeError("agent process supervisor closed during prepare")
            return durable

    def launch_and_wait(
        self,
        execution: AgentAttemptExecution,
        invocation: AgentProcessInvocation,
        permissions: PermissionDecider,
    ) -> AgentProcessCompletion:
        launch_request = _launch_request(invocation)
        launch_frame = encode_control_frame(launch_request)
        if len(launch_frame) > MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES:
            raise ValueError(
                "encoded agent launch exceeds "
                f"{MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES} bytes"
            )
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            owned = self._owned.get(execution.attempt_id)
            durable = self._store.load(execution.attempt_id)
            if (
                owned is None
                or durable.state is not AgentAttemptState.LAUNCH_ARMED
                or durable.process_owner_id != owned.owner
                or durable.watchdog_generation_id != owned.generation
            ):
                raise AgentProcessOwnerNotLocal
            conversation = invocation.conversation
            revision = None if conversation is None else conversation.executor_revision
            leads_attempt = _leads_attempt(owned, launch_frame, revision)
        if not leads_attempt:
            return self._await_shared_completion(owned)
        try:
            launch_response = self._request_with_retry(
                owned,
                launch_request,
                timeout_seconds=CONTROL_FRAME_TIMEOUT_SECONDS,
                maximum_response_bytes=MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES,
            )
            if launch_response.get("type") == "STARTED":
                owned.launched = True
                self._store.observe_process(execution, owned.owner, owned.generation)
            elif launch_response.get("type") == "TERMINAL_BEFORE_START":
                raise AgentProcessOwnerNotLocal
            else:
                raise RuntimeError("watchdog refused the exact launch")
            evidence = (
                _NOTHING_WAS_SAID
                if conversation is None
                else self._relay_conversation(owned, conversation, permissions)
            )
            wait_response = self._request_with_retry(
                owned,
                {"operation": "WAIT"},
                timeout_seconds=None,
                maximum_response_bytes=maximum_agent_wait_response_bytes(
                    invocation.command.standard_output_frame_bytes
                ),
            )
            completion = _completion_from_response(
                wait_response,
                invocation.command.standard_output_frame_bytes,
                evidence,
            )
        except Exception as error:
            with owned.condition:
                owned.failure = str(error) or type(error).__name__
                owned.owner_not_local_failure = isinstance(
                    error, AgentProcessOwnerNotLocal
                )
                owned.wait_completed.set()
                owned.condition.notify_all()
            raise
        with owned.condition:
            owned.completion = completion
            owned.wait_completed.set()
            owned.condition.notify_all()
            return completion

    def cancel(
        self,
        attempt: AgentAttempt,
        cause: ProviderCancellationCause = ProviderCancellationCause.OPERATOR,
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        lock = self._attempt_lock(attempt.attempt_id)
        with lock:
            owned = self._owned.get(attempt.attempt_id)
            if (
                owned is None
                or attempt.process_owner_id != owned.owner
                or attempt.watchdog_generation_id != owned.generation
            ):
                raise AgentProcessOwnerNotLocal
        return (
            AgentAttemptCancellationDisposition(self._stop_and_reap(owned, cause)),
            owned.owner,
            owned.generation,
        )

    def recover(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        """Attest what is left of an attempt this process never owned.

        A cgroup that is not there holds no process, so there is nothing to kill
        and the attestation stands on its own -- which is how `release` below has
        always read an absent directory. Requiring it to exist read the same
        absence as "ask again later", and later never came: a host that restarts
        its serving unit takes the whole subtree with it, so exactly the attempts
        a restart exists to converge were the ones it could never attest.
        """

        if attempt.process_owner_id is None or attempt.watchdog_generation_id is None:
            raise AgentProcessOwnerNotLocal
        lock = self._attempt_lock(attempt.attempt_id)
        with lock:
            if self._owned.get(attempt.attempt_id) is not None:
                raise AgentProcessOwnerNotLocal
            cgroup = self._cgroup_for(attempt)
            if cgroup.is_dir():
                _kill_cgroup_and_wait_empty(cgroup, self._ready_timeout_seconds)
        return (
            AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH,
            attempt.process_owner_id,
            attempt.watchdog_generation_id,
        )

    def release(self, attempt: AgentAttempt) -> None:
        if (
            attempt.state
            not in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}
            or attempt.process_phase is not AgentAttemptProcessPhase.CLEANUP_ATTESTED
            or attempt.cancellation is None
            or attempt.cancellation.disposition is None
        ):
            raise RuntimeError(
                "only durably attested cancellation cleanup can release its witness"
            )
        if attempt.process_owner_id is None or attempt.watchdog_generation_id is None:
            if (
                attempt.cancellation.disposition
                is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
            ):
                return
            raise RuntimeError("cleanup attestation has no watchdog identity")
        lock = self._attempt_lock(attempt.attempt_id)
        with lock:
            owned = self._owned.get(attempt.attempt_id)
            if owned is not None and (
                owned.owner != attempt.process_owner_id
                or owned.generation != attempt.watchdog_generation_id
            ):
                raise AgentProcessOwnerNotLocal
        if owned is not None:
            self._finalize_owned(attempt.attempt_id, owned)
            return
        with lock:
            cgroup = self._cgroup_for(attempt)
            if cgroup.is_dir():
                _kill_cgroup_and_wait_empty(cgroup, self._ready_timeout_seconds)
                cgroup.rmdir()
            (self._control_root / f"{attempt.attempt_id.value}.sock").unlink(
                missing_ok=True
            )

    def finalize(self, execution: AgentAttemptExecution) -> None:
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            owned = self._owned.get(execution.attempt_id)
            if owned is None:
                return
            terminal = self._store.load(execution.attempt_id)
            if terminal.state not in {
                AgentAttemptState.SUCCEEDED,
                AgentAttemptState.FAILED,
            }:
                raise RuntimeError("only a terminal attempt can release its watchdog")
        self._finalize_owned(execution.attempt_id, owned)

    def close(self) -> None:
        with self._registry_lock:
            self._closed = True
            owned = tuple(value for value in self._owned.values() if value is not None)
            self._owned.clear()
        for handle in owned:
            with handle.condition:
                handle.closed = True
                handle.condition.notify_all()
            self._detach_owned(handle)

    def _stop_and_reap(
        self, owned: _OwnedWatchdog, cause: ProviderCancellationCause | None
    ) -> str:
        """Stop this attempt's child now, and answer how it went out.

        The watchdog answers a cancellation only once the process is really
        gone -- the frame written, the signal sent, the group emptied, the
        child reaped -- so its answer is the attestation this seam hands on.
        Nothing here waits for the conversation: a relay is carrying frames
        through an authority that writes durable receipts, and a stop that
        waited for that would be a stop a slow disk could postpone. What the
        relay still owes is evidence, and evidence is collected afterwards.

        A stop with no cause is supervision's own: the relay broke and nobody
        chose this ending, so there is no word to carry to the conversation.
        """

        request: dict[str, object] = {"operation": "CANCEL"}
        if cause is not None:
            request["cause"] = cause.value
        response = self._request_with_retry(
            owned,
            request,
            timeout_seconds=(self._grace_seconds * 2) + 2,
            maximum_response_bytes=MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES,
        )
        if response.get("type") == "RECOVERY_HANDOFF":
            raise AgentProcessOwnerNotLocal
        if response.get("type") != "CANCELLED":
            raise RuntimeError("watchdog did not acknowledge cancellation")
        disposition_value = response.get("disposition")
        if type(disposition_value) is not str:
            raise RuntimeError("watchdog cancellation response is malformed")
        # The watchdog's own word, because only a caller that records a
        # disposition owes it the durable vocabulary; a fail-stop records none.
        return disposition_value

    def _finalize_owned(
        self, attempt_id: AgentAttemptId, owned: _OwnedWatchdog
    ) -> None:
        # Giving up the watchdog closes the endpoint the relay speaks through,
        # so a launch that is still collecting its conversation's last frames
        # is waited for here -- the one place where waiting for evidence costs
        # nobody a signal.
        if owned.launched and not owned.wait_completed.wait(
            timeout=self._ready_timeout_seconds
        ):
            raise RuntimeError("watchdog did not return the reaped process completion")
        deadline = time.monotonic() + self._ready_timeout_seconds
        with owned.condition:
            while owned.finalizing and not owned.finalized:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("watchdog finalization did not finish in bounds")
                owned.condition.wait(timeout=remaining)
            if owned.finalized:
                return
            owned.finalizing = True
        try:
            response = self._request_with_retry(
                owned,
                {"operation": "FINALIZE"},
                timeout_seconds=CONTROL_FRAME_TIMEOUT_SECONDS,
                maximum_response_bytes=MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES,
            )
            if response.get("type") != "FINALIZE_ACCEPTED":
                raise RuntimeError("watchdog refused terminal finalization")
            self._close_owned(owned)
        except Exception:
            with owned.condition:
                owned.finalizing = False
                owned.condition.notify_all()
            raise
        with owned.condition:
            owned.finalizing = False
            owned.finalized = True
            owned.closed = True
            owned.condition.notify_all()
        lock = self._attempt_lock(attempt_id)
        with lock:
            if self._owned.get(attempt_id) is owned:
                self._owned.pop(attempt_id, None)

    def _relay_conversation(
        self,
        owned: _OwnedWatchdog,
        conversation: ProviderConversationBinding,
        permissions: PermissionDecider,
    ) -> _ConversationEvidence:
        """Carry one conversation between the child and this run's authority.

        The whole duplex channel lives here, on the parent side of the control
        socket: the watchdog services pipes, deadlines and signals and never
        calls a conversation or a decider, so no provider's parser and no
        durable receipt write can sit between a cancellation and its signal.
        """

        relay = _ConversationRelay(conversation, permissions)
        try:
            relay.open()
            while True:
                response = self._request_with_retry(
                    owned,
                    relay.exchange_request(),
                    timeout_seconds=(
                        CONTROL_FRAME_TIMEOUT_SECONDS + EXCHANGE_HOLD_SECONDS
                    ),
                    maximum_response_bytes=MAXIMUM_AGENT_EXCHANGE_RESPONSE_BYTES,
                )
                if response.get("type") != "EXCHANGED":
                    raise RuntimeError(
                        "watchdog did not answer the conversation exchange"
                    )
                ending = relay.exchanged(response)
                if ending is not None:
                    return relay.ended(ending)
                requested = relay.cancellation_to_request()
                if requested is not None:
                    self._stop_and_reap(owned, requested)
        except AgentProcessOwnerNotLocal:
            raise
        except Exception:
            # Whatever the relay broke on -- a frame past its bound, an
            # authority that would not answer, a file access that refused to
            # open -- the child is still standing there waiting for a reply
            # nobody will send. It is stopped and reaped before this failure
            # surfaces, because everything the caller does next -- taking the
            # credential channel back, failing the attempt -- reads as though
            # the process it started were gone.
            self._stop_and_reap(owned, None)
            raise

    def _attempt_lock(self, attempt_id: AgentAttemptId) -> threading.Lock:
        with self._registry_lock:
            return self._attempt_locks.setdefault(attempt_id, threading.Lock())

    def _cgroup_for(self, attempt: AgentAttempt) -> Path:
        generation = attempt.watchdog_generation_id
        if generation is None:
            raise AgentProcessOwnerNotLocal
        return self._cgroup_root / (
            "atelier2-" + attempt.attempt_id.value[:16] + "-" + generation.value[:8]
        )

    def _wait_ready(self, owned: _OwnedWatchdog) -> None:
        if owned.process.stdout is None:
            raise RuntimeError("watchdog ready channel is absent")
        ready, _write, _error = select.select(
            [owned.process.stdout], [], [], self._ready_timeout_seconds
        )
        if not ready or owned.process.stdout.readline() != b"READY\n":
            detail = b""
            if owned.process.poll() is not None and owned.process.stderr is not None:
                detail = owned.process.stderr.read(2_001)
            raise RuntimeError(
                "watchdog did not prove readiness: "
                + detail.decode("utf-8", errors="replace")[:2_000]
            )
        if (
            not owned.endpoint.is_socket()
            or not (owned.cgroup / "cgroup.kill").is_file()
            or _cgroup_populated(owned.cgroup)
        ):
            raise RuntimeError("watchdog readiness attestation disagrees")

    @staticmethod
    def _await_shared_completion(owned: _OwnedWatchdog) -> AgentProcessCompletion:
        with owned.condition:
            while (
                owned.completion is None and owned.failure is None and not owned.closed
            ):
                owned.condition.wait()
            if owned.completion is not None:
                return owned.completion
            if owned.owner_not_local_failure or owned.closed:
                raise AgentProcessOwnerNotLocal
            raise RuntimeError(owned.failure or "shared process attempt failed")

    def _request_with_retry(
        self,
        owned: _OwnedWatchdog,
        request: dict[str, object],
        *,
        timeout_seconds: float | None,
        maximum_response_bytes: int,
    ) -> dict[str, object]:
        last_error: BaseException | None = None
        for retry in range(MAXIMUM_AGENT_CONTROL_REQUEST_ATTEMPTS):
            if retry:
                time.sleep(CONTROL_FRAME_TIMEOUT_SECONDS)
            try:
                response = self._request(
                    owned.endpoint,
                    request,
                    timeout_seconds=timeout_seconds,
                    maximum_response_bytes=maximum_response_bytes,
                )
                if len(encode_control_frame(response)) > maximum_response_bytes:
                    raise RuntimeError("watchdog response exceeds its exact bound")
                if response.get("type") in {
                    "BUSY",
                    "CONTROL_FRAME_TIMEOUT",
                    "FRAME_TOO_LARGE",
                    "MALFORMED",
                    "LAUNCH_MISMATCH",
                }:
                    raise RuntimeError("watchdog refused the exact control frame")
                return response
            except (OSError, TimeoutError, RuntimeError, ValueError) as error:
                last_error = error
        raise AgentProcessOwnerNotLocal from last_error

    @staticmethod
    def _request(
        endpoint: Path,
        request: dict[str, object],
        *,
        timeout_seconds: float | None = 30,
        maximum_response_bytes: int,
    ) -> dict[str, object]:
        frame = encode_control_frame(request)
        control_directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            short_endpoint = (
                Path("/proc/self/fd") / str(control_directory) / endpoint.name
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(CONTROL_FRAME_TIMEOUT_SECONDS)
                connection.connect(str(short_endpoint))
                connection.sendall(frame)
                connection.shutdown(socket.SHUT_WR)
                connection.settimeout(timeout_seconds)
                response_bytes = bytearray()
                while True:
                    chunk = connection.recv(
                        min(
                            65_536,
                            maximum_response_bytes + 1 - len(response_bytes),
                        )
                    )
                    if not chunk:
                        break
                    response_bytes.extend(chunk)
                    if len(response_bytes) > maximum_response_bytes:
                        raise RuntimeError("watchdog response exceeds its exact bound")
        finally:
            os.close(control_directory)
        try:
            response = json.loads(bytes(response_bytes).decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("watchdog response is malformed") from error
        if not isinstance(response, dict) or encode_control_frame(response) != bytes(
            response_bytes
        ):
            raise RuntimeError("watchdog response is noncanonical")
        return response

    def _close_owned(self, owned: _OwnedWatchdog) -> None:
        try:
            os.close(owned.owner_pipe)
        except OSError:
            pass
        try:
            owned.process.wait(timeout=self._ready_timeout_seconds)
        except subprocess.TimeoutExpired:
            owned.process.terminate()
            try:
                owned.process.wait(timeout=self._ready_timeout_seconds)
            except subprocess.TimeoutExpired:
                owned.process.kill()
                owned.process.wait()
        _close_watchdog_pipes(owned.process)
        owned.endpoint.unlink(missing_ok=True)
        if owned.cgroup.is_dir():
            owned.cgroup.rmdir()

    def _detach_owned(self, owned: _OwnedWatchdog) -> None:
        """Stop this owner while retaining evidence for durable recovery."""

        try:
            os.close(owned.owner_pipe)
        except OSError:
            pass
        try:
            owned.process.wait(timeout=(self._grace_seconds * 2) + 2)
        except subprocess.TimeoutExpired:
            _kill_cgroup_and_wait_empty(owned.cgroup, self._ready_timeout_seconds)
            owned.process.kill()
            owned.process.wait()
        _close_watchdog_pipes(owned.process)


def _close_watchdog_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def _launch_request(invocation: AgentProcessInvocation) -> dict[str, object]:
    command = invocation.command
    request: dict[str, object] = {
        "arguments": command.arguments,
        "environment": command.environment,
        "operation": "LAUNCH",
        "sandbox": sandbox_frame(command.sandbox),
        "standard_input": base64.b64encode(command.standard_input).decode("ascii"),
        "standard_output_frame_bytes": command.standard_output_frame_bytes,
        "working_directory": str(invocation.lease.working_directory),
        # The identity travels because the watchdog opens the path itself, later
        # and in another process: without it there is nothing to compare the
        # directory it finds against.
        "working_directory_identity": [invocation.lease.device, invocation.lease.inode],
    }
    # A print-mode launch names no conversation at all, so its frame stays
    # byte-for-byte the one this watchdog protocol has always carried.
    if invocation.conversation is not None:
        request["duplex"] = True
    return request


def _completion_from_response(
    response: dict[str, object],
    standard_output_frame_bytes: int,
    conversation: _ConversationEvidence = _NOTHING_WAS_SAID,
) -> AgentProcessCompletion:
    response_type = response.get("type")
    if response_type in {"RECOVERY_HANDOFF", "STOPPED"}:
        raise AgentProcessOwnerNotLocal
    if response_type != "COMPLETED":
        raise RuntimeError("watchdog did not return a process completion")
    return_code = response.get("return_code")
    standard_output_value = response.get("standard_output")
    standard_error_value = response.get("standard_error")
    if (
        type(return_code) is not int
        or type(standard_output_value) is not str
        or type(standard_error_value) is not str
    ):
        raise RuntimeError("watchdog process completion is malformed")
    try:
        standard_output = base64.b64decode(standard_output_value, validate=True)
        standard_error = base64.b64decode(standard_error_value, validate=True)
    except ValueError as error:
        raise RuntimeError("watchdog process completion is malformed") from error
    if (
        len(standard_output) > standard_output_frame_bytes
        or len(standard_error) > MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
    ):
        raise RuntimeError("watchdog process completion exceeds its exact bound")
    return AgentProcessCompletion(
        return_code,
        standard_output,
        standard_error,
        conversation.steps,
        conversation.outcome,
    )


def _cgroup_populated(cgroup: Path) -> bool:
    events = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
    return "populated 1" in events


def _kill_cgroup_and_wait_empty(cgroup: Path, timeout_seconds: float) -> None:
    (cgroup / "cgroup.kill").write_text("1", encoding="ascii")
    deadline = time.monotonic() + timeout_seconds
    while _cgroup_populated(cgroup) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _cgroup_populated(cgroup):
        raise RuntimeError("agent cgroup did not become empty in bounds")


def delegated_cgroup_root() -> Path:
    relative = next(
        (
            line.removeprefix("0::/").strip()
            for line in Path("/proc/self/cgroup")
            .read_text(encoding="ascii")
            .splitlines()
            if line.startswith("0::/")
        ),
        None,
    )
    if relative is None:
        raise RuntimeError("agent supervision requires cgroup v2")
    root = (Path("/sys/fs/cgroup") / relative).resolve()
    if not (root / "cgroup.procs").is_file() or not os.access(root, os.W_OK):
        raise RuntimeError("agent supervision requires a writable delegated cgroup")
    return root
