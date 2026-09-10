"""A whole supervised reading in an interpreter of its own, interrupted at the
one moment that would otherwise lose a process.

The interrupt has to be a real signal: what is under test is a signal arriving
between the reading process existing and this process holding its handle, and
only the kernel delivers one that way. A real SIGINT inside a test worker
lands on whichever thread that worker keeps unmasked, which is not the thread
running the reading -- so the reading runs here, in a process with one thread
and nothing else in it, and says on its own stdout what happened. The test
that starts this reads that line and nothing else.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence

from atelier2.host.instance_reader import ReadingBudget, supervised_reading

INTERRUPT_SEEN = "interrupt-seen"
INTERRUPT_MISSED = "interrupt-missed"
"""The two words this process can say: whether the interrupt it sent itself
came back out of the reading it interrupted. The number beside it is what the
reading process came back with -- `None` when nobody ever reaped it, which is
the very loss this stands against."""

_INTERRUPTED_READER = "tests.host.reader_entries.ignores_being_stopped"
"""A reading that would still be running if this supervision let go of it, so
a handle lost at the start leaves something to find."""

_UNREACHABLE_SERVICE = "http://127.0.0.1:8422"
"""Nothing is ever read: the interrupt lands before the gathering begins."""

_BUDGET = ReadingBudget(
    deadline_seconds=25.0,
    door_read_timeout_seconds=0.5,
    event_sample_read_timeout_seconds=2.0,
)

_SURVIVOR_STOP_SECONDS = 5.0
"""How long this process waits for a reading it had to end itself -- reached
only when the supervision lost its handle, and never left behind."""


def _interrupted_supervision() -> tuple[bool, int | None]:
    """Run one real supervision, send this process an interrupt the instant
    the reading process exists, and say what came of both.

    What the caller gets is the reading process's own ending as the
    supervision left it. A reading still running afterwards is ended here
    rather than left for the machine to find, but the answer already says it
    was never reaped.
    """

    started: list[subprocess.Popen[bytes]] = []
    open_a_process = subprocess.Popen

    def start_and_interrupt(
        command: Sequence[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        reader = open_a_process(
            command, stdin=stdin, stdout=stdout, stderr=stderr, pass_fds=pass_fds
        )
        started.append(reader)
        os.kill(os.getpid(), signal.SIGINT)
        return reader

    subprocess.Popen = start_and_interrupt
    interrupted = False
    try:
        supervised_reading(_INTERRUPTED_READER, _UNREACHABLE_SERVICE, _BUDGET)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        subprocess.Popen = open_a_process
    if not started:
        return interrupted, None
    ending = started[0].returncode
    if ending is None:
        started[0].kill()
        started[0].wait(timeout=_SURVIVOR_STOP_SECONDS)
    return interrupted, ending


if __name__ == "__main__":
    seen, returncode = _interrupted_supervision()
    print(f"{INTERRUPT_SEEN if seen else INTERRUPT_MISSED} {returncode}")
    raise SystemExit(0)
