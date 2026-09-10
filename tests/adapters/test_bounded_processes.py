"""`bounded_process_streams`/`bounded_process_answer` against real subprocesses.

`tests/adapters/test_agent_claim_cli.py` replaces this module's functions with
a double, so the boundary itself -- the selector loop, the byte bound, the
deadline, and reaping a process that outlives it -- needs its own proof
against a real child process.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from atelier2.adapters.bounded_processes import (
    BoundedProcessFailure,
    bounded_process_answer,
    bounded_process_streams,
)

TIMEOUT_SECONDS = 5.0
MAXIMUM_OUTPUT_BYTES = 4096


def _python_process(script: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        (sys.executable, "-c", script),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def test_bounded_process_streams_returns_both_streams_and_the_exit_code() -> None:
    process = _python_process(
        "import sys; sys.stdout.write('answer'); "
        "sys.stderr.write('diagnostic'); sys.exit(7)"
    )

    return_code, standard_output, standard_error = bounded_process_streams(
        process, TIMEOUT_SECONDS, MAXIMUM_OUTPUT_BYTES
    )

    assert return_code == 7
    assert standard_output == b"answer"
    assert standard_error == b"diagnostic"


def test_bounded_process_answer_returns_only_standard_output() -> None:
    process = _python_process(
        "import sys; sys.stdout.write('answer'); sys.stderr.write('noise')"
    )

    return_code, standard_output = bounded_process_answer(
        process, TIMEOUT_SECONDS, MAXIMUM_OUTPUT_BYTES
    )

    assert return_code == 0
    assert standard_output == b"answer"


def test_bounded_process_streams_refuses_output_over_the_byte_bound() -> None:
    process = _python_process("import sys; sys.stdout.write('x' * 64)")

    with pytest.raises(BoundedProcessFailure, match="more than 8 bytes"):
        bounded_process_streams(process, TIMEOUT_SECONDS, 8)

    assert process.wait(timeout=5) is not None


def test_bounded_process_streams_refuses_a_process_that_does_not_answer_in_time() -> (
    None
):
    """The deadline is already spent once this fires, so reaping races the
    kernel: whichever failure wins the race, the process is force-killed and
    the caller sees `BoundedProcessFailure`, never a hang.
    """

    process = _python_process("import time; time.sleep(30)")

    with pytest.raises(BoundedProcessFailure):
        bounded_process_streams(process, 0.05, MAXIMUM_OUTPUT_BYTES)

    assert process.wait(timeout=5) is not None
