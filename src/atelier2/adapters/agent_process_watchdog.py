from __future__ import annotations

import argparse
import base64
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from atelier2.adapters.leased_directory import entered_leased_directory
from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
)
from atelier2.ports.provider_conversations import ProviderCancellationCause

# The launch frame carries the process's whole standard input base64-encoded --
# four characters per three bytes -- beside its argv, environment and working
# directory. Twice the input bound is that expansion with the rest of the
# envelope's room left over, so every input this product admits can be launched.
MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES = 2 * MAXIMUM_AGENT_PROCESS_INPUT_BYTES
MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES = 4_096
CONTROL_FRAME_TIMEOUT_SECONDS = 1.0
# What one relay exchange carries back: the size of a single pipe read, so a
# child writing at full speed is drained in as many exchanges as it wrote
# reads, and no control frame ever holds more than one of them.
MAXIMUM_AGENT_EXCHANGE_OUTPUT_BYTES = 65_536
EXCHANGE_HOLD_SECONDS = CONTROL_FRAME_TIMEOUT_SECONDS
"""Reusing the control channel's patience keeps a silent conversation at one
round trip a second; the parent waits twice as long as this watchdog holds."""


def encode_control_frame(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def _announce_ready_on_standard_output() -> None:
    print("READY", flush=True)


_FRAMELESS_WAIT_ARMS = (
    "OUTPUT_LIMIT_EXCEEDED",
    "SUPERVISION_FAILED",
    "STOPPED",
    "RECOVERY_HANDOFF",
)
"""Every ending a wait answers with instead of a process's own frame."""

_CANCELLATION_CAUSES = frozenset(cause.value for cause in ProviderCancellationCause)
"""The words a cancellation may name itself with, and no others."""

_TERMINAL_CONTROL_SLOT = "TERMINAL_CONTROL"

_SLOT_OF_OPERATION = {
    "WAIT": "WAIT",
    "CANCEL": _TERMINAL_CONTROL_SLOT,
    "FINALIZE": _TERMINAL_CONTROL_SLOT,
    "LAUNCH": "LAUNCH_RETRY",
    "EXCHANGE": "EXCHANGE",
}
"""Which single-holder slot each control operation occupies while it runs."""

_EXCHANGE_ENDINGS = ("COMPLETED", *_FRAMELESS_WAIT_ARMS, *sorted(_CANCELLATION_CAUSES))
"""The wait vocabulary, plus a cancellation's own cause: a signal cannot say
afterwards why it was sent, so the canceller's own word carries through."""

MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES = max(
    len(encode_control_frame({"type": arm})) for arm in _FRAMELESS_WAIT_ARMS
)


def _base64_characters(byte_count: int) -> int:
    return 4 * ((byte_count + 2) // 3)


def maximum_agent_wait_response_bytes(standard_output_frame_bytes: int) -> int:
    """The exact wait-response bound for one invocation's declared frame."""

    empty_completion = encode_control_frame(
        {
            "return_code": -(2**31),
            "standard_error": "",
            "standard_output": "",
            "type": "COMPLETED",
        }
    )
    return max(
        MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES,
        len(empty_completion)
        + _base64_characters(standard_output_frame_bytes)
        + _base64_characters(MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES),
    )


MAXIMUM_AGENT_EXCHANGE_RESPONSE_BYTES = len(
    encode_control_frame(
        {
            "accepted_input_bytes": MAXIMUM_SIGNED_INT64,
            "ending": max(_EXCHANGE_ENDINGS, key=len),
            "standard_output": "",
            "type": "EXCHANGED",
        }
    )
) + _base64_characters(MAXIMUM_AGENT_EXCHANGE_OUTPUT_BYTES)
"""Measured, not spelled, so a raised read size or longer ending name grows
this bound with it instead of silently outgrowing a literal."""


class _CoordinatorState(StrEnum):
    READY = "READY"
    LAUNCHING = "LAUNCHING"
    RUNNING = "RUNNING"
    CANCEL_TERMINATING = "CANCEL_TERMINATING"
    OVERFLOW_TERMINATING = "OVERFLOW_TERMINATING"
    SUPERVISION_TERMINATING = "SUPERVISION_TERMINATING"
    OWNER_DEATH_TERMINATING = "OWNER_DEATH_TERMINATING"
    RECOVERY_HANDOFF = "RECOVERY_HANDOFF"
    TERMINATED = "TERMINATED"
    FINALIZING = "FINALIZING"


@dataclass
class _Connection:
    socket: socket.socket
    accepted_at: float
    input_bytes: bytearray = field(default_factory=bytearray)
    output_bytes: bytes | None = None
    output_offset: int = 0
    response_deadline: float | None = None
    slot: str = "UNCLASSIFIED"
    operation: str | None = None
    refuse_as_busy: bool = False


class Watchdog:
    """One selector-driven authority for a single provider generation."""

    def __init__(
        self,
        endpoint: Path,
        cgroup: Path,
        owner_pipe: int,
        grace: float,
    ) -> None:
        self._endpoint = endpoint
        self._cgroup = cgroup
        self._owner_pipe = owner_pipe
        self._grace = grace
        self._selector = selectors.DefaultSelector()
        self._server: socket.socket | None = None
        self._connections: dict[int, _Connection] = {}
        self._slots: dict[str, int] = {}
        self._state = _CoordinatorState.READY
        self._process: subprocess.Popen[bytes] | None = None
        self._provider_streams: dict[int, str] = {}
        self._standard_input = b""
        self._standard_input_offset = 0
        self._standard_input_watched = False
        self._duplex = False
        self._delivered_input_bytes = 0
        self._close_input_after_drain = False
        self._cancellation_frame = b""
        self._cancellation_cause: str | None = None
        self._pending_exchange: _Connection | None = None
        self._exchange_output_offset = 0
        self._exchange_deadline = 0.0
        self._standard_output = bytearray()
        self._standard_error = bytearray()
        self._standard_output_frame_bytes: int | None = None
        self._launch_replay: tuple[bytes, bytes] | None = None
        self._wait_response: bytes | None = None
        self._wait_arm: str | None = None
        self._cancel_response: bytes | None = None
        self._finalize_response: bytes | None = None
        self._termination_deadline: float | None = None
        self._termination_escalated = False
        self._termination_disposition: str | None = None
        self._termination_owner: str | None = None
        self._owner_dead = False

    def serve(self, announce_ready: Callable[[], None]) -> None:
        self._endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._endpoint.exists():
            raise RuntimeError("watchdog endpoint already exists")
        os.set_blocking(self._owner_pipe, False)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(str(self._endpoint))
            os.chmod(self._endpoint, 0o600)
            server.listen()
            server.setblocking(False)
            self._selector.register(server, selectors.EVENT_READ, "server")
            self._selector.register(self._owner_pipe, selectors.EVENT_READ, "owner")
            announce_ready()
            while self._state is not _CoordinatorState.FINALIZING:
                try:
                    self._tick()
                except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
                    self._fail_supervision(time.monotonic())
        finally:
            self._close_provider_descriptors()
            for connection in tuple(self._connections.values()):
                self._close_connection(connection)
            if self._server is not None:
                try:
                    self._selector.unregister(self._server)
                except (KeyError, ValueError):
                    pass
                self._server.close()
            try:
                self._selector.unregister(self._owner_pipe)
            except (KeyError, ValueError):
                pass
            os.close(self._owner_pipe)
            self._selector.close()

    def _fail_supervision(self, now: float) -> None:
        if self._termination_owner is None:
            self._begin_termination("SUPERVISION", now)
        elif not self._publish_recovery_handoff(now):
            try:
                self._drain_recovery_handoff()
            finally:
                self._state = _CoordinatorState.FINALIZING

    def _drain_recovery_handoff(self) -> None:
        """A tick dying in supervision never reaches a select worth calling,
        so this asks the selector directly: a failure here is not a delivery
        failure, so it keeps trying rather than dropping queued bytes, bounded
        by each connection's own existing deadline, not a new one."""

        while self._handoff_pending():
            now = time.monotonic()
            try:
                events = self._selector.select(self._next_timeout(now))
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
                events = ()
            for key, mask in events:
                if isinstance(key.data, _Connection) and mask & selectors.EVENT_WRITE:
                    self._write_connection(key.data, now)
            self._expire_connections(time.monotonic())

    def _handoff_pending(self) -> bool:
        return any(
            connection.output_bytes is not None
            for connection in self._connections.values()
            if connection.operation in {"WAIT", "CANCEL"}
        )

    def _tick(self) -> None:
        now = time.monotonic()
        self._expire_connections(now)
        self._advance_process(now)
        self._service_pending_exchange(now)
        events = self._selector.select(self._next_timeout(now))
        for key, mask in events:
            if key.data == "server":
                self._accept_connection(now)
            elif key.data == "owner":
                self._read_owner(now)
            elif isinstance(key.data, _Connection):
                self._service_connection(key.data, mask, now)
            else:
                self._service_provider(int(key.fd), str(key.data), mask, now)

    def _next_timeout(self, now: float) -> float:
        deadlines = [
            connection.response_deadline
            if connection.output_bytes is not None
            else (
                connection.accepted_at + CONTROL_FRAME_TIMEOUT_SECONDS
                if connection.operation is None
                else None
            )
            for connection in self._connections.values()
        ]
        deadlines.append(self._termination_deadline)
        finite = [deadline for deadline in deadlines if deadline is not None]
        if not finite:
            return 0.05
        return max(0.0, min(0.05, min(finite) - now))

    def _accept_connection(self, now: float) -> None:
        if self._server is None:
            return
        try:
            connection, _address = self._server.accept()
        except BlockingIOError:
            return
        connection.setblocking(False)
        state = _Connection(connection, now)
        descriptor = connection.fileno()
        if "UNCLASSIFIED" in self._slots:
            state.refuse_as_busy = True
            self._connections[descriptor] = state
            self._selector.register(connection, selectors.EVENT_READ, state)
            return
        self._slots["UNCLASSIFIED"] = descriptor
        self._connections[descriptor] = state
        self._selector.register(connection, selectors.EVENT_READ, state)

    def _read_owner(self, now: float) -> None:
        try:
            data = os.read(self._owner_pipe, 1)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            return
        try:
            self._selector.unregister(self._owner_pipe)
        except (KeyError, ValueError):
            pass
        self._owner_dead = True
        self._begin_termination("OWNER_DEATH", now)

    def _service_connection(self, state: _Connection, mask: int, now: float) -> None:
        if mask & selectors.EVENT_READ:
            self._read_connection(state, now)
        if state.socket.fileno() >= 0 and mask & selectors.EVENT_WRITE:
            self._write_connection(state, now)

    def _read_connection(self, state: _Connection, now: float) -> None:
        try:
            remaining = MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1 - len(state.input_bytes)
            chunk = state.socket.recv(max(1, min(65_536, remaining)))
        except BlockingIOError:
            return
        except OSError:
            self._close_connection(state)
            return
        if chunk:
            state.input_bytes.extend(chunk)
            if len(state.input_bytes) > MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES:
                response = "BUSY" if state.refuse_as_busy else "FRAME_TOO_LARGE"
                self._queue_response(state, {"type": response}, now)
            return
        self._classify_request(state, bytes(state.input_bytes), now)

    def _classify_request(self, state: _Connection, frame: bytes, now: float) -> None:
        try:
            request = json.loads(frame.decode("ascii"))
            if not isinstance(request, dict) or encode_control_frame(request) != frame:
                raise ValueError
            operation = request.get("operation")
            if not isinstance(operation, str):
                raise TypeError
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._queue_response(state, {"type": "MALFORMED"}, now)
            return
        slot = _SLOT_OF_OPERATION.get(operation)
        if state.refuse_as_busy and slot != _TERMINAL_CONTROL_SLOT:
            # A busy refusal is read to its end, not answered at the door,
            # because the relay reconnects per exchange: a cancellation racing
            # one of those connections would otherwise cost a retry -- a
            # second where nothing is signalled. One terminal control passes.
            self._queue_response(state, {"type": "BUSY"}, now)
            return
        if slot is None:
            self._queue_response(state, {"type": "MALFORMED"}, now)
            return
        descriptor = state.socket.fileno()
        self._release_slot(state)
        if slot in self._slots:
            self._queue_response(state, {"type": "BUSY"}, now)
            return
        self._slots[slot] = descriptor
        state.slot = slot
        state.operation = operation
        state.refuse_as_busy = False
        if operation == "LAUNCH":
            self._handle_launch(state, request, frame, now)
        elif operation == "WAIT":
            self._handle_wait(state, now)
        elif operation == "CANCEL":
            self._handle_cancel(state, request, now)
        elif operation == "EXCHANGE":
            self._handle_exchange(state, request, now)
        else:
            self._handle_finalize(state, now)
        if state.output_bytes is None:
            self._stop_reading(state)

    def _stop_reading(self, connection: _Connection) -> None:
        """Left registered, an end of file the kernel keeps reporting readable
        fires on every tick until there is a reply -- for a WAIT, that can be
        the run's whole remaining lifetime."""

        try:
            self._selector.unregister(connection.socket)
        except (KeyError, ValueError):
            pass

    def _handle_launch(
        self,
        connection: _Connection,
        request: dict[str, Any],
        frame: bytes,
        now: float,
    ) -> None:
        launch_replay = self._launch_replay
        if launch_replay is not None:
            launch_frame, launch_response = launch_replay
            response = (
                launch_response
                if frame == launch_frame
                else encode_control_frame({"type": "LAUNCH_MISMATCH"})
            )
            self._queue_encoded_response(connection, response, now)
            return
        if self._state is not _CoordinatorState.READY:
            launch_response = encode_control_frame(
                {
                    "outcome": "STOPPED",
                    "type": "TERMINAL_BEFORE_START",
                }
            )
            self._launch_replay = (frame, launch_response)
            self._publish_wait({"type": "STOPPED"}, now)
            self._termination_disposition = "NEVER_LAUNCHED"
            self._queue_encoded_response(connection, launch_response, now)
            return
        self._state = _CoordinatorState.LAUNCHING
        try:
            (
                arguments,
                working_directory,
                working_directory_identity,
                environment,
                standard_input,
                standard_output_frame_bytes,
                duplex,
            ) = _decode_launch_request(request)
            self._standard_output_frame_bytes = standard_output_frame_bytes
            self._duplex = duplex
            guarded = (
                sys.executable,
                "-m",
                "atelier2.adapters.agent_process_exec_guard",
                "--cgroup",
                str(self._cgroup),
                "--watchdog-pid",
                str(os.getpid()),
                "--",
                *arguments,
            )
            device, inode = working_directory_identity
            child_environment = {
                **os.environ,
                "ATELIER2_AGENT_ENVIRONMENT_B64": base64.b64encode(
                    json.dumps(
                        sorted(environment.items()), separators=(",", ":")
                    ).encode("utf-8")
                ).decode("ascii"),
            }
            with entered_leased_directory(Path(working_directory), device, inode) as (
                leased_cwd,
                leased_descriptor,
            ):
                process = subprocess.Popen(
                    guarded,
                    cwd=leased_cwd,
                    pass_fds=(leased_descriptor,),
                    env=child_environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            self._process = process
            self._standard_input = standard_input
            launch_response = encode_control_frame({"type": "STARTED"})
            try:
                self._configure_provider_descriptors(process)
            except (OSError, RuntimeError, ValueError):
                self._close_provider_descriptors()
                self._begin_termination("SUPERVISION", now)
            else:
                self._state = _CoordinatorState.RUNNING
        except (KeyError, OSError, subprocess.SubprocessError, TypeError, ValueError):
            self._close_provider_descriptors()
            launch_response = encode_control_frame(
                {
                    "outcome": "SUPERVISION_FAILED",
                    "type": "TERMINAL_BEFORE_START",
                }
            )
            self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
            self._publish_wait({"type": "SUPERVISION_FAILED"}, now)
        self._launch_replay = (frame, launch_response)
        self._queue_encoded_response(connection, launch_response, now)

    def _handle_wait(self, connection: _Connection, now: float) -> None:
        if self._wait_response is not None:
            self._queue_encoded_response(connection, self._wait_response, now)

    def _handle_cancel(
        self, connection: _Connection, request: dict[str, Any], now: float
    ) -> None:
        cause = request.get("cause")
        if cause is not None and cause not in _CANCELLATION_CAUSES:
            self._queue_response(connection, {"type": "MALFORMED"}, now)
            return
        if self._cancellation_cause is None and type(cause) is str:
            self._cancellation_cause = cause
        if self._cancel_response is not None:
            self._queue_encoded_response(connection, self._cancel_response, now)
            return
        if self._wait_response is not None:
            disposition = self._termination_disposition or "EXITED_BEFORE_SIGNAL"
            self._cancel_response = encode_control_frame(
                {"disposition": disposition, "type": "CANCELLED"}
            )
            self._queue_encoded_response(connection, self._cancel_response, now)
            return
        self._begin_termination("CANCEL", now)

    def _handle_exchange(
        self, connection: _Connection, request: dict[str, Any], now: float
    ) -> None:
        """Counting in cumulative bytes on both directions means a retried
        exchange delivers no byte twice: skip what is accepted, resend what is
        sent. It answers on the child's word, its end, or its hold expiring."""

        if not self._duplex:
            self._queue_response(connection, {"type": "MALFORMED"}, now)
            return
        try:
            delivered_output, input_offset, chunk, cancellation, close_input = (
                _decode_exchange_request(request)
            )
        except ValueError:
            self._queue_response(connection, {"type": "MALFORMED"}, now)
            return
        if input_offset > self._delivered_input_bytes:
            self._queue_response(connection, {"type": "MALFORMED"}, now)
            return
        fresh = chunk[self._delivered_input_bytes - input_offset :]
        if fresh:
            if (
                self._unwritten_input_bytes() + len(fresh)
                > MAXIMUM_AGENT_PROCESS_INPUT_BYTES
            ):
                self._queue_response(connection, {"type": "INPUT_LIMIT_EXCEEDED"}, now)
                return
            self._accept_standard_input(fresh)
        if cancellation:
            self._cancellation_frame = cancellation
        if close_input:
            self._close_input_after_drain = True
            self._close_drained_standard_input()
        self._exchange_output_offset = min(delivered_output, len(self._standard_output))
        if self._pending_exchange is not connection:
            self._pending_exchange = connection
            self._exchange_deadline = now + EXCHANGE_HOLD_SECONDS
        self._service_pending_exchange(now)

    def _service_pending_exchange(self, now: float) -> None:
        connection = self._pending_exchange
        if connection is None:
            return
        available = bytes(
            self._standard_output[
                self._exchange_output_offset : self._exchange_output_offset
                + MAXIMUM_AGENT_EXCHANGE_OUTPUT_BYTES
            ]
        )
        ending = self._exchange_ending()
        if not available and ending is None and now < self._exchange_deadline:
            return
        self._pending_exchange = None
        self._queue_response(
            connection,
            {
                "accepted_input_bytes": self._written_input_bytes(),
                "ending": ending or "",
                "standard_output": base64.b64encode(available).decode("ascii"),
                "type": "EXCHANGED",
            },
            now,
        )

    def _exchange_ending(self) -> str | None:
        """A reaped child answers `COMPLETED` even when a cancellation reaped
        it, so termination is asked first: output a cancellation stopped is
        not output that simply ran out."""

        if self._wait_response is None:
            return None
        if self._termination_owner != "CANCEL":
            return self._wait_arm
        return self._cancellation_cause or "STOPPED"

    def _handle_finalize(self, connection: _Connection, now: float) -> None:
        if self._finalize_response is not None:
            self._queue_encoded_response(connection, self._finalize_response, now)
            return
        if self._wait_response is None:
            self._queue_response(connection, {"type": "FINALIZE_REFUSED"}, now)
            return
        self._finalize_response = encode_control_frame({"type": "FINALIZE_ACCEPTED"})
        self._queue_encoded_response(connection, self._finalize_response, now)

    def _configure_provider_descriptors(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("provider pipes are absent")
        for stream, role, events in (
            (process.stdin, "stdin", selectors.EVENT_WRITE),
            (process.stdout, "stdout", selectors.EVENT_READ),
            (process.stderr, "stderr", selectors.EVENT_READ),
        ):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            self._provider_streams[descriptor] = role
            self._selector.register(descriptor, events, role)
        self._standard_input_watched = True
        if not self._standard_input:
            self._rest_standard_input()

    def _service_provider(
        self, descriptor: int, role: str, _mask: int, now: float
    ) -> None:
        try:
            if role == "stdin":
                self._write_standard_input(descriptor)
            else:
                self._read_provider_output(descriptor, role, now)
        except BrokenPipeError:
            if role == "stdin":
                self._close_provider_stream(role)
            else:
                self._fail_provider_stream(role, now)
        except OSError:
            self._fail_provider_stream(role, now)

    def _fail_provider_stream(self, role: str, now: float) -> None:
        self._close_provider_stream(role)
        self._begin_termination("SUPERVISION", now)

    def _write_standard_input(self, descriptor: int) -> None:
        try:
            written = os.write(
                descriptor, self._standard_input[self._standard_input_offset :]
            )
        except BlockingIOError:
            return
        self._standard_input_offset += written
        if self._standard_input_offset == len(self._standard_input):
            self._rest_standard_input()

    def _read_provider_output(self, descriptor: int, role: str, now: float) -> None:
        try:
            chunk = os.read(descriptor, 65_536)
        except BlockingIOError:
            return
        if not chunk:
            self._close_provider_stream(role)
            return
        if self._termination_owner is not None:
            return
        target = self._standard_output if role == "stdout" else self._standard_error
        target.extend(chunk)
        declared_frame_bytes = self._standard_output_frame_bytes
        if declared_frame_bytes is None:
            raise RuntimeError("provider output arrived before its declared frame")
        limit = (
            declared_frame_bytes
            if role == "stdout"
            else MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        )
        if len(target) > limit:
            self._standard_output.clear()
            self._standard_error.clear()
            self._drop_unwritten_input()
            self._close_provider_stream("stdin")
            self._begin_termination("OVERFLOW", now)

    def _advance_process(self, now: float) -> None:
        if self._state is _CoordinatorState.TERMINATED:
            return
        process = self._process
        if process is None:
            return
        return_code = process.poll()
        if self._termination_owner is None:
            if (
                return_code is not None
                and self._provider_output_closed()
                and not _cgroup_populated(self._cgroup)
            ):
                process.wait()
                self._publish_process_completion(now)
            return
        if self._state is _CoordinatorState.RECOVERY_HANDOFF:
            return
        if (
            process.poll() is not None
            and not _cgroup_populated(self._cgroup)
            and self._provider_output_closed()
        ):
            process.wait()
            self._finish_termination(now)
            return
        if self._termination_deadline is not None and now >= self._termination_deadline:
            self._escalate_termination(now)

    def _begin_termination(self, owner: str, now: float) -> None:
        if self._termination_owner is not None:
            if owner == "OWNER_DEATH" and self._wait_response is not None:
                self._state = _CoordinatorState.FINALIZING
            return
        self._termination_owner = owner
        self._termination_escalated = False
        if owner == "CANCEL":
            self._write_cancellation_frame()
        self._drop_unwritten_input()
        self._close_provider_stream("stdin")
        if self._process is None:
            self._termination_disposition = "NEVER_LAUNCHED"
            if owner == "CANCEL":
                self._publish_wait({"type": "STOPPED"}, now)
                self._publish_cancel(now)
            elif owner == "OWNER_DEATH":
                self._state = _CoordinatorState.FINALIZING
            else:
                arm = (
                    "OUTPUT_LIMIT_EXCEEDED"
                    if owner == "OVERFLOW"
                    else "SUPERVISION_FAILED"
                )
                self._publish_wait({"type": arm}, now)
            return
        self._state = {
            "CANCEL": _CoordinatorState.CANCEL_TERMINATING,
            "OVERFLOW": _CoordinatorState.OVERFLOW_TERMINATING,
            "SUPERVISION": _CoordinatorState.SUPERVISION_TERMINATING,
            "OWNER_DEATH": _CoordinatorState.OWNER_DEATH_TERMINATING,
        }[owner]
        if self._process.poll() is not None and not _cgroup_populated(self._cgroup):
            self._termination_disposition = "EXITED_BEFORE_SIGNAL"
            if self._provider_output_closed():
                self._process.wait()
                self._finish_termination(now)
                return
            self._termination_deadline = now + self._grace
            return
        signalled = False
        if self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
                signalled = True
            except ProcessLookupError:
                pass
        if signalled:
            self._termination_disposition = "REAPED_AFTER_TERM"
        self._termination_deadline = now + self._grace

    def _escalate_termination(self, now: float) -> None:
        if self._termination_escalated:
            self._publish_recovery_handoff(now)
            return
        self._termination_escalated = True
        self._termination_deadline = None
        if _cgroup_populated(self._cgroup):
            (self._cgroup / "cgroup.kill").write_text("1", encoding="ascii")
            self._termination_disposition = "REAPED_AFTER_KILL"
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._termination_deadline = now + max(1.0, self._grace)

    def _finish_termination(self, now: float) -> None:
        owner = self._termination_owner
        if owner == "OWNER_DEATH" or self._owner_dead:
            self._state = _CoordinatorState.FINALIZING
            return
        if owner == "CANCEL":
            self._publish_process_completion(now)
            self._publish_cancel(now)
            return
        if owner == "OVERFLOW":
            self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
            self._publish_wait({"type": "OUTPUT_LIMIT_EXCEEDED"}, now)
            self._publish_cancel(now)
            return
        self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
        self._publish_wait({"type": "SUPERVISION_FAILED"}, now)
        self._publish_cancel(now)

    def _publish_recovery_handoff(self, now: float) -> bool:
        if self._state is _CoordinatorState.RECOVERY_HANDOFF:
            return False
        encoded = encode_control_frame({"type": "RECOVERY_HANDOFF"})
        self._wait_response = encoded
        self._wait_arm = "RECOVERY_HANDOFF"
        self._cancel_response = encoded
        self._termination_deadline = None
        self._state = _CoordinatorState.RECOVERY_HANDOFF
        self._close_provider_descriptors()
        for connection in tuple(self._connections.values()):
            if (
                connection.operation in {"WAIT", "CANCEL"}
                and connection.output_bytes is None
            ):
                self._queue_encoded_response(connection, encoded, now)
        return True

    def _publish_process_completion(self, now: float) -> None:
        process = self._process
        if process is None or process.returncode is None:
            raise RuntimeError("provider completion has no reaped return code")
        self._publish_wait(
            {
                "return_code": int(process.returncode),
                "standard_error": base64.b64encode(self._standard_error).decode(
                    "ascii"
                ),
                "standard_output": base64.b64encode(self._standard_output).decode(
                    "ascii"
                ),
                "type": "COMPLETED",
            },
            now,
        )

    def _publish_cancel(self, now: float) -> None:
        disposition = self._termination_disposition or "EXITED_BEFORE_SIGNAL"
        self._cancel_response = encode_control_frame(
            {"disposition": disposition, "type": "CANCELLED"}
        )
        for connection in tuple(self._connections.values()):
            if connection.operation == "CANCEL" and connection.output_bytes is None:
                self._queue_encoded_response(connection, self._cancel_response, now)

    def _publish_wait(self, response: dict[str, object], now: float) -> None:
        if self._wait_response is not None:
            return
        encoded = encode_control_frame(response)
        declared_frame_bytes = self._standard_output_frame_bytes
        bound = (
            MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES
            if declared_frame_bytes is None
            else maximum_agent_wait_response_bytes(declared_frame_bytes)
        )
        if len(encoded) > bound:
            raise RuntimeError("watchdog wait response exceeds its exact bound")
        self._wait_response = encoded
        self._wait_arm = str(response["type"])
        self._state = _CoordinatorState.TERMINATED
        for connection in tuple(self._connections.values()):
            if connection.operation == "WAIT" and connection.output_bytes is None:
                self._queue_encoded_response(connection, encoded, now)

    def _expire_connections(self, now: float) -> None:
        for connection in tuple(self._connections.values()):
            if connection.output_bytes is None:
                if (
                    connection.operation is None
                    and now >= connection.accepted_at + CONTROL_FRAME_TIMEOUT_SECONDS
                ):
                    response = (
                        "BUSY" if connection.refuse_as_busy else "CONTROL_FRAME_TIMEOUT"
                    )
                    self._queue_response(connection, {"type": response}, now)
            elif (
                connection.response_deadline is not None
                and now >= connection.response_deadline
            ):
                self._close_connection(connection)

    def _queue_response(
        self, connection: _Connection, response: dict[str, object], now: float
    ) -> None:
        self._queue_encoded_response(connection, encode_control_frame(response), now)

    def _queue_encoded_response(
        self, connection: _Connection, response: bytes, now: float
    ) -> None:
        connection.output_bytes = response
        connection.output_offset = 0
        connection.response_deadline = now + CONTROL_FRAME_TIMEOUT_SECONDS
        self._stop_reading(connection)
        try:
            self._selector.register(
                connection.socket, selectors.EVENT_WRITE, connection
            )
        except (KeyError, ValueError):
            self._close_connection(connection)

    def _write_connection(self, connection: _Connection, _now: float) -> None:
        response = connection.output_bytes
        if response is None:
            return
        try:
            sent = connection.socket.send(response[connection.output_offset :])
        except BlockingIOError:
            return
        except OSError:
            self._close_connection(connection)
            return
        connection.output_offset += sent
        if connection.output_offset == len(response):
            self._close_connection(connection)

    def _release_slot(self, connection: _Connection) -> None:
        descriptor = connection.socket.fileno()
        if self._slots.get(connection.slot) == descriptor:
            self._slots.pop(connection.slot, None)
        connection.slot = ""

    def _close_connection(self, connection: _Connection) -> None:
        if self._pending_exchange is connection:
            self._pending_exchange = None
        descriptor = connection.socket.fileno()
        self._release_slot(connection)
        self._connections.pop(descriptor, None)
        try:
            self._selector.unregister(connection.socket)
        except (KeyError, ValueError):
            pass
        connection.socket.close()

    def _rest_standard_input(self) -> None:
        """A print-mode child, told everything at once, is finished once
        drained; a conversation's child hears more later, so its pipe stays
        open -- unwatched, since nothing to send would wake the selector."""

        if not self._duplex:
            self._close_provider_stream("stdin")
            return
        self._standard_input = b""
        self._standard_input_offset = 0
        if self._close_input_after_drain:
            self._close_drained_standard_input()
            return
        descriptor = self._standard_input_descriptor()
        if descriptor is None or not self._standard_input_watched:
            return
        self._standard_input_watched = False
        try:
            self._selector.unregister(descriptor)
        except (KeyError, ValueError):
            pass

    def _accept_standard_input(self, chunk: bytes) -> None:
        unwritten = self._standard_input[self._standard_input_offset :]
        self._standard_input = unwritten + chunk
        self._standard_input_offset = 0
        self._delivered_input_bytes += len(chunk)
        self._watch_standard_input()

    def _unwritten_input_bytes(self) -> int:
        return len(self._standard_input) - self._standard_input_offset

    def _drop_unwritten_input(self) -> None:
        """A stop drops what the child never took, uncounting it: told those
        bytes were acknowledged, a conversation would believe an answer
        arrived that in fact went nowhere.
        """

        self._delivered_input_bytes -= self._unwritten_input_bytes()
        self._standard_input = b""
        self._standard_input_offset = 0

    def _written_input_bytes(self) -> int:
        """What a conversation is held to its input bound by: bytes only
        buffered here still count as the relay's, or the executor's declared
        bound would end at a pipe a child never reads while the backlog grows
        behind it. A launch's own payload is written first, so it counts
        before the relay's.
        """

        return max(0, self._delivered_input_bytes - self._unwritten_input_bytes())

    def _close_drained_standard_input(self) -> None:
        if self._close_input_after_drain and not self._unwritten_input_bytes():
            self._standard_input_watched = False
            self._close_provider_stream("stdin")

    def _watch_standard_input(self) -> None:
        descriptor = self._standard_input_descriptor()
        if descriptor is None or self._standard_input_watched:
            return
        self._standard_input_watched = True
        self._selector.register(descriptor, selectors.EVENT_WRITE, "stdin")

    def _standard_input_descriptor(self) -> int | None:
        return next(
            (fd for fd, role in self._provider_streams.items() if role == "stdin"),
            None,
        )

    def _write_cancellation_frame(self) -> None:
        """Composed while the conversation still ran, stopping costs no round
        trip through it. What does not fit the pipe now is dropped, not waited
        for: the signal in this same turn is the real stop, and waiting on a
        full pipe would let a stuck child postpone its own cancellation.
        """

        descriptor = self._standard_input_descriptor()
        if descriptor is None or not self._cancellation_frame:
            return
        frame = self._cancellation_frame
        self._cancellation_frame = b""
        try:
            os.write(descriptor, frame)
        except OSError:
            pass

    def _provider_output_closed(self) -> bool:
        return not {"stdout", "stderr"} & set(self._provider_streams.values())

    def _close_provider_stream(self, role: str) -> None:
        descriptor = next(
            (fd for fd, current in self._provider_streams.items() if current == role),
            None,
        )
        if descriptor is None:
            return
        self._provider_streams.pop(descriptor, None)
        try:
            self._selector.unregister(descriptor)
        except (KeyError, ValueError):
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _close_provider_descriptors(self) -> None:
        for role in tuple(self._provider_streams.values()):
            self._close_provider_stream(role)


def _decode_launch_request(
    request: dict[str, Any],
) -> tuple[tuple[str, ...], str, tuple[int, int], dict[str, str], bytes, int, bool]:
    # `duplex` is named only by a conversation launch, so a print-mode frame
    # stays byte-for-byte the one this watchdog has always been given.
    if set(request) - {"duplex"} != {
        "arguments",
        "environment",
        "operation",
        "standard_input",
        "standard_output_frame_bytes",
        "working_directory",
        "working_directory_identity",
    }:
        # An older-build watchdog refuses a request naming the identity rather
        # than launching unchecked: the net under a mixed deploy, not the norm.
        raise ValueError("launch request has unexpected fields")
    arguments_value = request["arguments"]
    if (
        type(arguments_value) is not list
        or not arguments_value
        or any(type(value) is not str or not value for value in arguments_value)
    ):
        raise ValueError("launch arguments are malformed")
    working_directory_value = request["working_directory"]
    if (
        type(working_directory_value) is not str
        or not Path(working_directory_value).is_absolute()
    ):
        raise ValueError("launch working directory is malformed")
    identity_value = request["working_directory_identity"]
    if (
        type(identity_value) is not list
        or len(identity_value) != 2
        or any(type(value) is not int or value < 0 for value in identity_value)
    ):
        raise ValueError("launch working directory identity is malformed")
    environment_value = request["environment"]
    if type(environment_value) is not list:
        raise ValueError("launch environment is malformed")
    environment_pairs: list[tuple[str, str]] = []
    for pair in environment_value:
        if (
            type(pair) is not list
            or len(pair) != 2
            or type(pair[0]) is not str
            or not pair[0]
            or type(pair[1]) is not str
        ):
            raise ValueError("launch environment is malformed")
        environment_pairs.append((pair[0], pair[1]))
    environment = dict(environment_pairs)
    if len(environment) != len(environment_pairs):
        raise ValueError("launch environment names are duplicated")
    standard_input_value = request["standard_input"]
    if type(standard_input_value) is not str:
        raise ValueError("launch standard input is malformed")
    standard_input = base64.b64decode(standard_input_value, validate=True)
    if len(standard_input) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES:
        raise ValueError("launch standard input exceeds its exact bound")
    standard_output_frame_bytes = request["standard_output_frame_bytes"]
    if type(standard_output_frame_bytes) is not int or standard_output_frame_bytes < 1:
        raise ValueError("launch standard output frame is malformed")
    duplex = request.get("duplex", False)
    if type(duplex) is not bool:
        raise ValueError("launch conversation flag is malformed")
    return (
        tuple(arguments_value),
        working_directory_value,
        (identity_value[0], identity_value[1]),
        environment,
        standard_input,
        standard_output_frame_bytes,
        duplex,
    )


def _decode_exchange_request(
    request: dict[str, Any],
) -> tuple[int, int, bytes, bytes, bool]:
    """One relay exchange: what the parent has taken, and what it hands over."""

    if set(request) != {
        "cancellation_frame",
        "close_input",
        "delivered_output_bytes",
        "operation",
        "standard_input",
        "standard_input_offset",
    }:
        raise ValueError("exchange request has unexpected fields")
    delivered_output_bytes = request["delivered_output_bytes"]
    input_offset = request["standard_input_offset"]
    if (
        type(delivered_output_bytes) is not int
        or delivered_output_bytes < 0
        or type(input_offset) is not int
        or input_offset < 0
    ):
        raise ValueError("exchange offsets are malformed")
    standard_input_value = request["standard_input"]
    cancellation_value = request["cancellation_frame"]
    if type(standard_input_value) is not str or type(cancellation_value) is not str:
        raise ValueError("exchange payloads are malformed")
    standard_input = base64.b64decode(standard_input_value, validate=True)
    cancellation_frame = base64.b64decode(cancellation_value, validate=True)
    if (
        len(standard_input) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES
        or len(cancellation_frame) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES
    ):
        raise ValueError("exchange payload exceeds its exact bound")
    close_input = request["close_input"]
    if type(close_input) is not bool:
        raise ValueError("exchange input closure is malformed")
    return (
        delivered_output_bytes,
        input_offset,
        standard_input,
        cancellation_frame,
        close_input,
    )


def _cgroup_populated(cgroup: Path) -> bool:
    events = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
    return "populated 1" in events


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atelier2-agent-watchdog")
    parser.add_argument("--endpoint", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--owner-pipe", type=int, required=True)
    parser.add_argument("--grace", type=float, required=True)
    parsed = parser.parse_args(arguments)
    Watchdog(
        parsed.endpoint,
        parsed.cgroup,
        parsed.owner_pipe,
        parsed.grace,
    ).serve(_announce_ready_on_standard_output)


if __name__ == "__main__":
    main()
