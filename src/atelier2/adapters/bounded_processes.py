import os
import selectors
import signal
import subprocess
import time
from typing import IO


class BoundedProcessFailure(OSError): ...


def bounded_process_answer(
    process: subprocess.Popen[bytes], timeout_seconds: float, maximum_output_bytes: int
) -> tuple[int, bytes]:
    """What a process answered on standard output, under one bound and deadline."""

    return_code, standard_output, _diagnostics = bounded_process_streams(
        process, timeout_seconds, maximum_output_bytes
    )
    return return_code, standard_output


def bounded_process_streams(
    process: subprocess.Popen[bytes], timeout_seconds: float, maximum_output_bytes: int
) -> tuple[int, bytes, bytes]:
    """Both streams of a process, each under the same byte bound and deadline.

    Most callers want the answer alone and take it through
    `bounded_process_answer`. A caller reads the diagnostic stream too where
    what it has to tell apart is only said there -- a program that refused its
    arguments against one that accepted them, for instance.
    """

    if process.stdout is None or process.stderr is None:
        raise BoundedProcessFailure("bounded process has no readable streams")
    deadline = time.monotonic() + timeout_seconds
    try:
        standard_output, standard_error = _read_bounded_streams(
            process.stdout, process.stderr, deadline, maximum_output_bytes
        )
        return_code = _awaited_return_code(process, deadline)
        return return_code, bytes(standard_output), bytes(standard_error)
    finally:
        _reap_process(process, deadline)


def _read_bounded_streams(
    stdout: IO[bytes], stderr: IO[bytes], deadline: float, maximum_output_bytes: int
) -> tuple[bytearray, bytearray]:
    """Both streams, read until each closes, none exceeding the byte bound,
    none outliving the deadline."""

    standard_output = bytearray()
    standard_error = bytearray()
    with selectors.DefaultSelector() as selector:
        for stream, output in ((stdout, standard_output), (stderr, standard_error)):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, output)
        while selector.get_map():
            _read_ready_streams(selector, deadline, maximum_output_bytes)
    return standard_output, standard_error


def _read_ready_streams(
    selector: selectors.BaseSelector, deadline: float, maximum_output_bytes: int
) -> None:
    try:
        ready_streams = selector.select(max(0, deadline - time.monotonic()))
    except OverflowError as error:
        raise BoundedProcessFailure("process deadline failed") from error
    if not ready_streams:
        raise BoundedProcessFailure("process did not answer in time")
    for ready, _events in ready_streams:
        output = ready.data
        chunk = os.read(ready.fd, maximum_output_bytes + 1 - len(output))
        if not chunk:
            selector.unregister(ready.fd)
            continue
        output += chunk
        if len(output) > maximum_output_bytes:
            raise BoundedProcessFailure(
                f"bounded process answered with more than {maximum_output_bytes} bytes"
            )


def _awaited_return_code(process: subprocess.Popen[bytes], deadline: float) -> int:
    try:
        return process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        raise BoundedProcessFailure("bounded process did not answer in time") from error


def _reap_process(process: subprocess.Popen[bytes], deadline: float) -> None:
    try:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.kill()
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    raise BoundedProcessFailure("bounded process remained") from error
                raise
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise BoundedProcessFailure("bounded process tree remained") from error
    finally:
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
