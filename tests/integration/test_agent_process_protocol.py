from __future__ import annotations

import base64
import json
import os
import select
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from atelier2.adapters import agent_process_watchdog as watchdog_module
from atelier2.adapters import agent_processes as process_module
from atelier2.adapters.agent_process_watchdog import (
    CONTROL_FRAME_TIMEOUT_SECONDS,
    MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES,
    MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES,
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
    Watchdog,
    encode_control_frame,
    maximum_agent_wait_response_bytes,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    MAXIMUM_SIGNED_INT64,
    AgentExecutionResult,
)
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessOwnerNotLocal,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    NOTHING_IS_PERMITTED,
    SCENARIO_PROVIDER_FRAME_BYTES,
    RecordingAgentExecutorV2,
    agent_attempt_execution,
    launching,
    process_invocation,
    runtime_workspace_owner,
)

_PROVIDER_WRITES_EXACT_BYTES = """
import os, sys
frame = b"o" * int(sys.argv[1])
while frame:
    frame = frame[os.write(1, frame):]
"""

_PROVIDER_EMITS_PADDED_ENVELOPE = """
import json, os, sys
frame = json.dumps({"padding": "x" * int(sys.argv[1]), "result": sys.argv[2]}).encode()
while frame:
    frame = frame[os.write(1, frame):]
"""


def _decode_padded_envelope(
    completion: AgentProcessCompletion,
) -> AgentExecutionResult:
    envelope = json.loads(completion.standard_output.decode("utf-8"))
    return AgentExecutionResult(json.dumps(envelope["result"]).encode("utf-8"))


def _padded_envelope_executor(
    declared_frame_bytes: int, padding_bytes: int, result: str = "ok"
) -> RecordingAgentExecutorV2:
    """A provider carrying a small result inside a frame it declares itself."""

    return RecordingAgentExecutorV2(
        command=launching(
            sys.executable,
            "-c",
            _PROVIDER_EMITS_PADDED_ENVELOPE,
            str(padding_bytes),
            result,
            frame_bytes=declared_frame_bytes,
        ),
        decoder=_decode_padded_envelope,
    )


@pytest.mark.parametrize("termination_owner", (None, "CANCEL"))
def test_terminal_publication_is_one_shot_and_reuses_cached_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    termination_owner: str | None,
) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    owner_pipe, owner_writer = os.pipe()
    process = subprocess.Popen((sys.executable, "-c", "pass"))
    process.wait(timeout=5)
    watchdog = Watchdog(tmp_path / "control.sock", cgroup, owner_pipe, 0.1)
    watchdog._process = process
    watchdog._standard_output_frame_bytes = SCENARIO_PROVIDER_FRAME_BYTES
    watchdog._standard_output.extend(b"output")
    watchdog._standard_error.extend(b"error")
    watchdog._termination_owner = termination_owner
    watchdog._termination_disposition = "EXITED_BEFORE_SIGNAL"
    try:
        watchdog._advance_process(1.0)
        cached_wait = watchdog._wait_response
        cached_cancel = watchdog._cancel_response
        assert cached_wait is not None
        if termination_owner == "CANCEL":
            assert cached_cancel is not None

        def reject_reconstruction(_now: float) -> None:
            raise AssertionError("terminal response was reconstructed")

        monkeypatch.setattr(
            watchdog, "_publish_process_completion", reject_reconstruction
        )
        monkeypatch.setattr(watchdog, "_publish_cancel", reject_reconstruction)

        watchdog._advance_process(2.0)

        assert watchdog._wait_response is cached_wait
        assert watchdog._cancel_response is cached_cancel
    finally:
        watchdog._selector.close()
        os.close(owner_pipe)
        os.close(owner_writer)


def test_recovery_handoff_publication_and_retries_reuse_cached_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(tmp_path / "control.sock", tmp_path / "cgroup", owner_pipe, 0.1)
    peers: list[socket.socket] = []
    try:
        watchdog._publish_recovery_handoff(1.0)
        cached_wait = watchdog._wait_response
        cached_cancel = watchdog._cancel_response
        assert cached_wait is not None
        assert cached_cancel is cached_wait

        def reject_reconstruction(_payload: dict[str, object]) -> bytes:
            raise AssertionError("recovery handoff was reconstructed")

        monkeypatch.setattr(
            watchdog_module, "encode_control_frame", reject_reconstruction
        )
        watchdog._publish_recovery_handoff(2.0)

        for operation, handler, cached in (
            ("WAIT", watchdog._handle_wait, cached_wait),
            (
                "CANCEL",
                lambda connection, now: watchdog._handle_cancel(
                    connection, {"operation": "CANCEL"}, now
                ),
                cached_cancel,
            ),
        ):
            server, peer = socket.socketpair()
            peers.append(peer)
            server.setblocking(False)
            connection = watchdog_module._Connection(
                server, 2.0, slot=operation, operation=operation
            )
            watchdog._connections[server.fileno()] = connection
            watchdog._slots[operation] = server.fileno()
            watchdog._selector.register(server, selectors.EVENT_READ, connection)

            handler(connection, 2.0)

            assert connection.output_bytes is cached
            watchdog._close_connection(connection)
    finally:
        for peer in peers:
            peer.close()
        watchdog._selector.close()
        os.close(owner_pipe)
        os.close(owner_writer)


def test_serve_reaches_finalizing_after_bounded_persistent_tick_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `_tick()` that never stops raising must not spin `serve()` forever.

    The bound is asserted from inside the fake tick itself: past it, the fake
    raises `AssertionError` instead of `OSError`, so a coordinator that never
    reaches `FINALIZING` fails this test promptly instead of hanging it.
    """

    endpoint = tmp_path / "control.sock"
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, tmp_path / "cgroup", owner_pipe, 0.1)
    tick_calls = 0
    maximum_tolerated_ticks = 10

    def persistently_failing_tick() -> None:
        nonlocal tick_calls
        tick_calls += 1
        if tick_calls > maximum_tolerated_ticks:
            raise AssertionError(
                "serve() kept ticking past the bound without reaching FINALIZING"
            )
        raise OSError("descriptor of the long-dead child is gone")

    monkeypatch.setattr(watchdog, "_tick", persistently_failing_tick)
    try:
        watchdog.serve(lambda: None)
    finally:
        os.close(owner_writer)

    assert watchdog._state is watchdog_module._CoordinatorState.FINALIZING
    assert tick_calls <= maximum_tolerated_ticks


def test_provider_stream_error_deregisters_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider descriptor that errors is stopped watching, not left to refire.

    Left registered, a descriptor that keeps reporting a read error stays
    selector-ready forever, so every following tick would service it again
    for free -- the same failure, at no cost in wall time, spinning the loop.
    """

    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(tmp_path / "control.sock", tmp_path / "cgroup", owner_pipe, 0.1)
    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    watchdog._provider_streams[read_end] = "stdout"
    watchdog._selector.register(read_end, selectors.EVENT_READ, "stdout")
    real_read = os.read

    def fail_on_the_dead_descriptor(descriptor: int, size: int) -> bytes:
        if descriptor == read_end:
            raise OSError("bad file descriptor")
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", fail_on_the_dead_descriptor)
    try:
        watchdog._service_provider(read_end, "stdout", selectors.EVENT_READ, 1.0)

        assert read_end not in watchdog._provider_streams
        with pytest.raises(KeyError):
            watchdog._selector.get_key(read_end)
    finally:
        watchdog._selector.close()
        os.close(owner_pipe)
        os.close(owner_writer)
        os.close(write_end)
        try:
            os.close(read_end)
        except OSError:
            pass


def test_running_watchdog_bounds_four_control_roles_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = tmp_path / "control.sock"
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    cgroup_events = cgroup / "cgroup.events"
    cgroup_events.write_text("populated 1\n", encoding="ascii")
    provider_ready = tmp_path / "provider-ready"
    provider = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import signal,sys; from pathlib import Path; signal.signal(signal.SIGTERM, lambda *_: None); Path(sys.argv[1]).touch()\nwhile True:\n signal.pause()",
            str(provider_ready),
        ),
        start_new_session=True,
    )
    _wait_until(provider_ready.exists)
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, cgroup, owner_pipe, 5.0)
    watchdog._process = provider
    watchdog._standard_output_frame_bytes = SCENARIO_PROVIDER_FRAME_BYTES
    launch_admitted = threading.Event()

    def hold_launch_slot(
        connection: watchdog_module._Connection,
        _request: dict[str, object],
        _frame: bytes,
        _now: float,
    ) -> None:
        watchdog._selector.unregister(connection.socket)
        launch_admitted.set()

    monkeypatch.setattr(watchdog, "_handle_launch", hold_launch_slot)
    errors: list[Exception] = []
    thread = _start_wire_watchdog(watchdog, endpoint, errors)
    clients: list[socket.socket] = []
    owner_open = True
    try:
        launch = _send_without_reading(
            endpoint, encode_control_frame({"operation": "LAUNCH"})
        )
        clients.append(launch)
        assert launch_admitted.wait(timeout=2)

        waiting = _send_without_reading(
            endpoint, encode_control_frame({"operation": "WAIT"})
        )
        clients.append(waiting)
        _wait_until(lambda: "WAIT" in watchdog._slots)

        cancelling = _send_without_reading(
            endpoint, encode_control_frame({"operation": "CANCEL"})
        )
        clients.append(cancelling)
        _wait_until(lambda: "TERMINAL_CONTROL" in watchdog._slots)

        for operation in ("LAUNCH", "WAIT", "FINALIZE"):
            assert _request_control_bytes(
                endpoint, encode_control_frame({"operation": operation})
            ) == encode_control_frame({"type": "BUSY"})

        unclassified = _connect_control(endpoint)
        clients.append(unclassified)
        _wait_until(lambda: "UNCLASSIFIED" in watchdog._slots)
        assert _request_control_bytes(
            endpoint, encode_control_frame({"operation": "WAIT"})
        ) == encode_control_frame({"type": "BUSY"})

        os.killpg(provider.pid, signal.SIGKILL)
        provider.wait(timeout=5)
        cgroup_events.write_text("populated 0\n", encoding="ascii")
        assert _receive_control(waiting)["type"] == "COMPLETED"
        assert _receive_control(cancelling)["type"] == "CANCELLED"
        os.close(owner_writer)
        owner_open = False
        thread.join(timeout=5)
    finally:
        if provider.poll() is None:
            os.killpg(provider.pid, signal.SIGKILL)
            provider.wait(timeout=5)
            cgroup_events.write_text("populated 0\n", encoding="ascii")
        for client in clients:
            client.close()
        if owner_open:
            if thread.is_alive():
                _wait_until(lambda: watchdog._wait_response is not None)
            os.close(owner_writer)
        thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)
    assert not thread.is_alive()
    assert errors == []


def test_running_watchdog_times_out_a_stalled_response_then_replays_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = tmp_path / "control.sock"
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    owner_pipe, owner_writer = os.pipe()
    process = subprocess.Popen((sys.executable, "-c", "pass"))
    process.wait(timeout=5)
    watchdog = Watchdog(endpoint, cgroup, owner_pipe, 0.1)
    watchdog._process = process
    watchdog._standard_error.extend(b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES)
    watchdog._standard_output.extend(b"o" * SCENARIO_PROVIDER_FRAME_BYTES)
    watchdog._standard_output_frame_bytes = SCENARIO_PROVIDER_FRAME_BYTES
    handle_wait = watchdog._handle_wait

    def constrain_response_buffer(
        connection: watchdog_module._Connection, now: float
    ) -> None:
        connection.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1_024)
        handle_wait(connection, now)

    monkeypatch.setattr(watchdog, "_handle_wait", constrain_response_buffer)
    expected = encode_control_frame(
        {
            "return_code": process.returncode,
            "standard_error": base64.b64encode(
                b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
            ).decode("ascii"),
            "standard_output": base64.b64encode(
                b"o" * SCENARIO_PROVIDER_FRAME_BYTES
            ).decode("ascii"),
            "type": "COMPLETED",
        }
    )
    assert MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES < len(expected)
    assert len(expected) <= maximum_agent_wait_response_bytes(
        SCENARIO_PROVIDER_FRAME_BYTES
    )
    errors: list[Exception] = []
    thread = _start_wire_watchdog(watchdog, endpoint, errors)
    owner_open = True
    try:
        with _send_without_reading(
            endpoint, encode_control_frame({"operation": "WAIT"})
        ) as stalled:
            readable, _writable, _failed = select.select((stalled,), (), (), 2)
            assert readable == [stalled]
            assert _request_control_bytes(
                endpoint, encode_control_frame({"operation": "WAIT"})
            ) == encode_control_frame({"type": "BUSY"})
            replayed = _request_until_not_busy(
                endpoint, encode_control_frame({"operation": "WAIT"})
            )
            assert replayed == expected
        os.close(owner_writer)
        owner_open = False
        thread.join(timeout=5)
    finally:
        if thread.is_alive():
            if owner_open:
                os.close(owner_writer)
            thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)
    assert not thread.is_alive()
    assert errors == []


def test_watchdog_fails_loud_if_wait_response_exceeds_protocol_bound(
    tmp_path: Path,
) -> None:
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(tmp_path / "control.sock", tmp_path / "cgroup", owner_pipe, 0.1)
    try:
        with pytest.raises(RuntimeError, match="wait response exceeds"):
            watchdog._publish_wait(
                {"detail": "x" * MAXIMUM_AGENT_FRAMELESS_WAIT_RESPONSE_BYTES}, 1.0
            )

        assert watchdog._wait_response is None
    finally:
        watchdog._selector.close()
        os.close(owner_pipe)
        os.close(owner_writer)


@pytest.mark.parametrize(
    ("contender_frame", "closes_input"),
    (
        pytest.param(
            encode_control_frame({"operation": "WAIT"}), True, id="complete-eof"
        ),
        pytest.param(b"{", False, id="read-timeout"),
        pytest.param(
            b"x" * (MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1),
            False,
            id="request-overflow",
        ),
    ),
)
def test_unclassified_busy_reply_survives_every_bounded_contender_exit(
    running_wire_watchdog: tuple[Watchdog, Path],
    contender_frame: bytes,
    closes_input: bool,
) -> None:
    watchdog, endpoint = running_wire_watchdog
    with _connect_control(endpoint):
        _wait_until(lambda: "UNCLASSIFIED" in watchdog._slots)

        with _connect_control(endpoint) as contender:
            contender.settimeout(CONTROL_FRAME_TIMEOUT_SECONDS * 2)
            contender.sendall(contender_frame)
            if closes_input:
                contender.shutdown(socket.SHUT_WR)

            assert _receive_control_bytes(contender) == encode_control_frame(
                {"type": "BUSY"}
            )


def test_a_cancellation_is_admitted_while_another_connection_holds_the_slot(
    running_wire_watchdog: tuple[Watchdog, Path],
) -> None:
    """A stop never waits out the connection a relay exchange is opening.

    The duplex relay reconnects for every exchange, so a cancellation racing
    one of those connections would meet the busy refusal and cost a retry --
    a second in which nothing has been signalled.
    """

    watchdog, endpoint = running_wire_watchdog
    with _connect_control(endpoint):
        _wait_until(lambda: "UNCLASSIFIED" in watchdog._slots)

        answer = _request_control_bytes(
            endpoint, encode_control_frame({"operation": "CANCEL"})
        )

    assert answer == encode_control_frame(
        {"disposition": "NEVER_LAUNCHED", "type": "CANCELLED"}
    )


@pytest.mark.parametrize("_startup", range(5))
def test_repeated_wire_watchdog_start_returns_only_after_listener_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _startup: int,
) -> None:
    endpoint = tmp_path / "control.sock"
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, tmp_path / "cgroup", owner_pipe, 0.1)
    original_listen = socket.socket.listen
    listen_entered = threading.Event()
    allow_listener = threading.Event()
    errors: list[Exception] = []
    watchdog_threads: list[threading.Thread] = []

    def pause_listener(server: socket.socket, *arguments: int) -> None:
        listen_entered.set()
        assert allow_listener.wait(timeout=2)
        original_listen(server, *arguments)

    monkeypatch.setattr(socket.socket, "listen", pause_listener)

    def start() -> None:
        watchdog_threads.append(_start_wire_watchdog(watchdog, endpoint, errors))

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert listen_entered.wait(timeout=2)
        assert endpoint.is_socket()
        assert starter.is_alive()
        with pytest.raises(ConnectionRefusedError), _connect_control(endpoint):
            pass

        allow_listener.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert len(watchdog_threads) == 1
        with _connect_control(endpoint):
            pass
    finally:
        allow_listener.set()
        os.close(owner_writer)
        starter.join(timeout=2)
        for thread in watchdog_threads:
            thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)
    assert errors == []
    assert all(not thread.is_alive() for thread in watchdog_threads)


def test_supervisor_drains_exactly_bounded_outputs_after_closed_input(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "process/io"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        provider = """
import os
import threading

os.close(0)
barrier = threading.Barrier(3)

def write_all(descriptor, value):
    barrier.wait()
    remaining = memoryview(value)
    while remaining:
        remaining = remaining[os.write(descriptor, remaining):]

threads = (
    threading.Thread(target=write_all, args=(1, b'o' * 49152)),
    threading.Thread(target=write_all, args=(2, b'e' * 49152)),
)
for thread in threads:
    thread.start()
barrier.wait()
for thread in threads:
    thread.join()
"""
        invocation = process_invocation(
            execution.attempt_id,
            (sys.executable, "-c", provider),
            Path.cwd(),
            standard_input=b"i" * MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)

        completion = supervisor.launch_and_wait(
            execution, invocation, NOTHING_IS_PERMITTED
        )

        assert completion.standard_output == b"o" * SCENARIO_PROVIDER_FRAME_BYTES
        assert (
            completion.standard_error
            == b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        )
        # The draining frame's own bytes are what this test measures; the
        # attempt's recorded output is a fixed valid-JSON value instead of the
        # frame itself, since a completed V3 attempt now proves its output
        # against a schema and the frame's raw repeated character is not one.
        terminal = store.complete_success(execution, AgentExecutionResult(b'"drained"'))
        assert isinstance(terminal, AgentAttemptSucceeded)
        supervisor.finalize(execution)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "declared_frame_bytes",
    (0, -1, 1.0, MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES + 1),
    ids=("zero", "negative", "not-an-integer", "above-port-bound"),
)
def test_a_command_without_a_positive_declared_frame_is_refused(
    declared_frame_bytes: Any,
) -> None:
    with pytest.raises(ValueError, match="standard output frame"):
        AgentProcessCommand(
            (sys.executable, "-c", "pass"),
            standard_output_frame_bytes=declared_frame_bytes,
        )


@pytest.mark.parametrize(
    "return_code", (-MAXIMUM_SIGNED_INT64 - 2, MAXIMUM_SIGNED_INT64 + 1)
)
def test_process_completion_refuses_a_return_code_outside_signed_int64(
    return_code: int,
) -> None:
    with pytest.raises(ValueError, match="return code"):
        AgentProcessCompletion(return_code, b"", b"")


def test_the_wait_response_bound_is_exactly_the_declared_frame_at_its_worst() -> None:
    declared_frame_bytes = 8_192
    worst_case = encode_control_frame(
        {
            "return_code": -(2**31),
            "standard_error": base64.b64encode(
                b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
            ).decode("ascii"),
            "standard_output": base64.b64encode(b"o" * declared_frame_bytes).decode(
                "ascii"
            ),
            "type": "COMPLETED",
        }
    )

    assert maximum_agent_wait_response_bytes(declared_frame_bytes) == len(worst_case)


@pytest.mark.parametrize("excess_bytes", (0, 1), ids=("at-the-bound", "one-byte-over"))
def test_supervision_admits_the_declared_frame_and_refuses_one_byte_more(
    tmp_path: Path, excess_bytes: int
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    declared_frame_bytes = 8_192
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, f"frame/edge/{excess_bytes}")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        invocation = process_invocation(
            execution.attempt_id,
            (
                sys.executable,
                "-c",
                _PROVIDER_WRITES_EXACT_BYTES,
                str(declared_frame_bytes + excess_bytes),
            ),
            Path.cwd(),
            standard_output_frame_bytes=declared_frame_bytes,
        )
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)

        if excess_bytes:
            with pytest.raises(RuntimeError, match="did not return a process"):
                supervisor.launch_and_wait(execution, invocation, NOTHING_IS_PERMITTED)
        else:
            completion = supervisor.launch_and_wait(
                execution, invocation, NOTHING_IS_PERMITTED
            )
            assert completion.standard_output == b"o" * declared_frame_bytes
    finally:
        runtime.close()


def test_supervision_holds_each_provider_to_its_own_declared_frame(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    padding_bytes = MAXIMUM_AGENT_OUTPUT_BYTES_V2 + 16_384
    try:
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        generous = _padded_envelope_executor(2 * padding_bytes, padding_bytes)
        frugal = _padded_envelope_executor(4_096, padding_bytes)
        generous_execution = agent_attempt_execution(
            attempt_request(runtime, "frame/generous")
        )
        frugal_execution = agent_attempt_execution(
            attempt_request(runtime, "frame/frugal")
        )

        workspaces = runtime_workspace_owner(runtime)
        accepted = execute_agent_attempt(
            generous_execution,
            generous,
            store,
            runtime.agent_process_supervisor,
            workspaces,
            permissions=GRANTS_NOTHING,
        )
        with pytest.raises(RuntimeError, match="did not return a process"):
            execute_agent_attempt(
                frugal_execution,
                frugal,
                store,
                runtime.agent_process_supervisor,
                workspaces,
                permissions=GRANTS_NOTHING,
            )

        assert isinstance(accepted, AgentAttemptSucceeded)
        observed_frame = generous.completions[0].standard_output
        assert generous.results == [AgentExecutionResult(b'"ok"')]
        assert len(generous.released_commands) == 1
        assert len(observed_frame) > MAXIMUM_AGENT_OUTPUT_BYTES_V2
        assert frugal.results == []
        assert len(frugal.released_commands) == 1
        assert (
            store.load(frugal_execution.attempt_id).state
            is not AgentAttemptState.SUCCEEDED
        )
    finally:
        runtime.close()


def test_lost_control_replies_replay_without_launching_twice(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/lost-launch-reply")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        counter = tmp_path / "provider-count"
        invocation = process_invocation(
            execution.attempt_id,
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import os,sys; Path(sys.argv[1]).open('ab').write(b'x'); Path(sys.argv[2]).open('rb', buffering=0).read(1); os.write(1,b'\"done\"')",
                str(counter),
                str(tmp_path / "provider-release"),
            ),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        os.mkfifo(tmp_path / "provider-release")
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        owned = supervisor._owned[execution.attempt_id]
        assert owned is not None
        launch_frame = encode_control_frame(process_module._launch_request(invocation))
        lost_launch = _send_without_reading(owned.endpoint, launch_frame)
        ready_launches, _writable, _failed = select.select((lost_launch,), (), (), 5)
        assert ready_launches == [lost_launch]
        started = _request_control_bytes(owned.endpoint, launch_frame)
        assert started == encode_control_frame({"type": "STARTED"})
        assert _request_control_bytes(owned.endpoint, launch_frame) == started
        partial_wait = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "WAIT"})
        )
        with (tmp_path / "provider-release").open("wb", buffering=0) as release:
            release.write(b"x")
        assert partial_wait.recv(8) == b'{"return'
        partial_wait.close()

        completion = supervisor.launch_and_wait(
            execution, invocation, NOTHING_IS_PERMITTED
        )

        assert completion.standard_output == b'"done"'
        assert counter.read_bytes() == b"x"
        terminal = store.complete_success(
            execution, AgentExecutionResult(completion.standard_output)
        )
        assert isinstance(terminal, AgentAttemptSucceeded)
        lost_finalize = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "FINALIZE"})
        )
        ready_finalizers, _writable, _failed = select.select(
            (lost_finalize,), (), (), 5
        )
        assert ready_finalizers == [lost_finalize]
        finalized = _request_control_bytes(
            owned.endpoint, encode_control_frame({"operation": "FINALIZE"})
        )
        assert finalized == encode_control_frame({"type": "FINALIZE_ACCEPTED"})
        assert (
            _request_control_bytes(
                owned.endpoint, encode_control_frame({"operation": "FINALIZE"})
            )
            == finalized
        )
        supervisor.finalize(execution)
        lost_launch.close()
        lost_finalize.close()
    finally:
        runtime.close()


def test_control_slots_bound_bad_peers_while_cancel_progresses_beside_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/control-slots")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        owned = supervisor._owned[execution.attempt_id]
        assert owned is not None

        with _connect_control(owned.endpoint) as incomplete:
            incomplete.sendall(b'{"operation"')
            assert _receive_control(incomplete) == {"type": "CONTROL_FRAME_TIMEOUT"}
        with _send_without_reading(
            owned.endpoint, b'{"operation": "WAIT"}'
        ) as noncanonical:
            assert _receive_control(noncanonical) == {"type": "MALFORMED"}
        with _send_without_reading(
            owned.endpoint, b"x" * (MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1)
        ) as oversized:
            assert _receive_control(oversized) == {"type": "FRAME_TOO_LARGE"}
        waits = tuple(
            _send_without_reading(
                owned.endpoint, encode_control_frame({"operation": "WAIT"})
            )
            for _index in range(2)
        )
        ready_waits, _writable, _failed = select.select(waits, (), (), 2)
        assert len(ready_waits) == 1
        competing_wait = ready_waits[0]
        waiting = waits[1] if competing_wait is waits[0] else waits[0]
        with competing_wait:
            assert _receive_control(competing_wait) == {"type": "BUSY"}

        counter = tmp_path / "provider-count"
        lost_cancel = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "CANCEL"})
        )
        with waiting:
            assert _receive_control(waiting) == {"type": "STOPPED"}
        invocation = process_invocation(
            execution.attempt_id,
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                str(counter),
            ),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        terminal_frame = encode_control_frame(
            process_module._launch_request(invocation)
        )
        terminal_before_start = _request_control_bytes(owned.endpoint, terminal_frame)
        assert terminal_before_start == encode_control_frame(
            {"outcome": "STOPPED", "type": "TERMINAL_BEFORE_START"}
        )
        assert (
            _request_control_bytes(owned.endpoint, terminal_frame)
            == terminal_before_start
        )
        with pytest.raises(AgentProcessOwnerNotLocal):
            supervisor.launch_and_wait(execution, invocation, NOTHING_IS_PERMITTED)
        assert not counter.exists()

        attempt = store.load(execution.attempt_id)
        command = CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            "cancel-beside-wait",
            attempt.state_version,
            AgentAttemptReplacement.NONE,
        )
        store.request_cancellation(command)
        blocked = _connect_control(owned.endpoint)
        cancel_frame = encode_control_frame({"operation": "CANCEL"})
        blocked.sendall(cancel_frame[:-1])
        request_once = supervisor._request
        cancel_requests = 0

        def count_cancel_requests(
            endpoint: Path,
            request: dict[str, object],
            *,
            timeout_seconds: float | None = 30,
            maximum_response_bytes: int,
        ) -> dict[str, object]:
            nonlocal cancel_requests
            if request.get("operation") == "CANCEL":
                cancel_requests += 1
            return request_once(
                endpoint,
                request,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
            )

        monkeypatch.setattr(supervisor, "_request", count_cancel_requests)
        disposition, owner, generation = supervisor.cancel(
            store.load(execution.attempt_id)
        )

        lost_cancel.close()
        with blocked:
            assert _receive_control(blocked) == {"type": "CONTROL_FRAME_TIMEOUT"}
        # A peer holding the door with half a frame delays nobody's stop: the
        # cancellation is admitted on its first try and the bad peer is still
        # answered on its own bound.
        assert cancel_requests == 1
        assert disposition is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
        terminal = store.attest_cancellation_cleanup(
            command, disposition, owner, generation
        )
        supervisor.release(terminal.attempt)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("peer_phase", "peer_response"),
    (
        ("connect", None),
        ("send", b""),
        ("partial-read", b'{"type":'),
        ("decode-noncanonical", b'{"type": "STARTED"}'),
        (
            "decode-oversized",
            b"x" * (MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES + 1),
        ),
    ),
)
def test_real_transport_failures_retain_exact_durable_launch_authority(
    tmp_path: Path,
    peer_phase: str,
    peer_response: bytes | None,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, f"process/transport/{peer_phase}")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        owned = supervisor._owned[execution.attempt_id]
        assert owned is not None
        real_endpoint = owned.endpoint
        peer_endpoint = real_endpoint.with_name(f"peer-{peer_phase}.sock")
        requests: list[bytes] = []
        peer_errors: list[BaseException] = []
        peer_thread = (
            _serve_control_responses(
                peer_endpoint, peer_response, requests, peer_errors
            )
            if peer_response is not None
            else None
        )
        invocation = process_invocation(
            execution.attempt_id,
            (sys.executable, "-c", "pass"),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        owned.endpoint = peer_endpoint
        try:
            with pytest.raises(AgentProcessOwnerNotLocal):
                supervisor.launch_and_wait(execution, invocation, NOTHING_IS_PERMITTED)
        finally:
            owned.endpoint = real_endpoint
            if peer_thread is not None:
                peer_thread.join(timeout=5)
                peer_endpoint.unlink(missing_ok=True)

        if peer_thread is not None:
            assert not peer_thread.is_alive()
            assert peer_errors == []
            assert (
                requests
                == [encode_control_frame(process_module._launch_request(invocation))]
                * 2
            )
        assert supervisor._owned[execution.attempt_id] is owned
        assert owned.process.poll() is None
        assert real_endpoint.is_socket()
        assert owned.launched is False
        durable = store.load(execution.attempt_id)
        assert durable.state is AgentAttemptState.LAUNCH_ARMED
        assert durable.process_phase is AgentAttemptProcessPhase.LAUNCH_AUTHORIZED
        assert durable.process_owner_id == owned.owner
        assert durable.watchdog_generation_id == owned.generation

        command = CancelAgentAttemptRequest(
            durable.run_id,
            durable.attempt_id,
            "cleanup-transport-uncertainty",
            durable.state_version,
            AgentAttemptReplacement.NONE,
        )
        store.request_cancellation(command)
        disposition, owner, generation = supervisor.cancel(
            store.load(execution.attempt_id)
        )
        terminal = store.attest_cancellation_cleanup(
            command, disposition, owner, generation
        )
        supervisor.release(terminal.attempt)
    finally:
        runtime.close()


def _serve_control_responses(
    endpoint: Path,
    response: bytes,
    requests: list[bytes],
    errors: list[BaseException],
) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        control_directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            short_endpoint = (
                Path("/proc/self/fd") / str(control_directory) / endpoint.name
            )
            server.bind(str(short_endpoint))
            server.listen()
            server.settimeout(5)
            ready.set()
            for _retry in range(process_module.MAXIMUM_AGENT_CONTROL_REQUEST_ATTEMPTS):
                with server.accept()[0] as connection:
                    connection.settimeout(2)
                    request = bytearray()
                    while chunk := connection.recv(65_536):
                        request.extend(chunk)
                    requests.append(bytes(request))
                    if response:
                        connection.sendall(response)
        except OSError as error:
            errors.append(error)
            ready.set()
        finally:
            server.close()
            os.close(control_directory)

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2)
    assert errors == []
    return thread


def _start_wire_watchdog(
    watchdog: Watchdog, endpoint: Path, errors: list[Exception]
) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        try:
            watchdog.serve(ready.set)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2)
    assert errors == []
    assert endpoint.is_socket()
    return thread


@pytest.fixture
def running_wire_watchdog(tmp_path: Path) -> Iterator[tuple[Watchdog, Path]]:
    endpoint = tmp_path / "control.sock"
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, tmp_path / "cgroup", owner_pipe, 0.1)
    errors: list[Exception] = []
    thread = _start_wire_watchdog(watchdog, endpoint, errors)
    try:
        yield watchdog, endpoint
    finally:
        os.close(owner_writer)
        thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)
    assert not thread.is_alive()
    assert errors == []


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("watchdog did not reach the expected wire state")
        time.sleep(0.005)


def _send_without_reading(endpoint: Path, frame: bytes) -> socket.socket:
    connection = _connect_control(endpoint)
    connection.sendall(frame)
    connection.shutdown(socket.SHUT_WR)
    return connection


def _request_control_bytes(endpoint: Path, frame: bytes) -> bytes:
    with _send_without_reading(endpoint, frame) as connection:
        return _receive_control_bytes(connection)


def _request_until_not_busy(endpoint: Path, frame: bytes) -> bytes:
    busy = encode_control_frame({"type": "BUSY"})
    deadline = time.monotonic() + (CONTROL_FRAME_TIMEOUT_SECONDS * 3)
    while True:
        response = _request_control_bytes(endpoint, frame)
        if response != busy:
            return response
        if time.monotonic() >= deadline:
            raise AssertionError("control role did not release in bounds")
        time.sleep(0.005)


def _receive_control_bytes(connection: socket.socket) -> bytes:
    response = bytearray()
    while chunk := connection.recv(65_536):
        response.extend(chunk)
    return bytes(response)


def _connect_control(endpoint: Path) -> socket.socket:
    control_directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        short_endpoint = Path("/proc/self/fd") / str(control_directory) / endpoint.name
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2)
        connection.connect(str(short_endpoint))
        return connection
    finally:
        os.close(control_directory)


def _receive_control(connection: socket.socket) -> dict[str, object]:
    response_bytes = bytearray()
    while chunk := connection.recv(
        MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES + 1 - len(response_bytes)
    ):
        response_bytes.extend(chunk)
        if len(response_bytes) > MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES:
            raise AssertionError("control response exceeded its test bound")
    response = json.loads(bytes(response_bytes).decode("ascii"))
    assert isinstance(response, dict)
    assert encode_control_frame(response) == bytes(response_bytes)
    return response


def test_a_launch_request_without_the_directory_identity_is_refused_by_name() -> None:
    """A request from an older build is refused rather than launched unchecked.

    Both sides of this protocol land together, so this is the net under a mixed
    state and not the ordinary path: a watchdog that accepted the older shape
    would start a provider in a directory nobody compared against the lease.
    """

    older = {
        "arguments": ["/bin/provider"],
        "environment": [],
        "operation": "LAUNCH",
        "standard_input": "",
        "standard_output_frame_bytes": 17,
        "working_directory": "/leased",
    }

    with pytest.raises(ValueError, match="unexpected fields"):
        watchdog_module._decode_launch_request(older)
