from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from collections.abc import Sequence
from pathlib import Path

from atelier2.contracts.sandbox_grants import (
    LEASED_DIRECTORY_BIND,
    LEASED_DIRECTORY_DESCRIPTOR,
)

_PR_SET_PDEATHSIG = 1
_SIGKILL = 9
_ENVIRONMENT_CHANNEL = "ATELIER2_AGENT_ENVIRONMENT_B64"


def guarded_exec(
    *,
    cgroup: Path,
    watchdog_pid: int,
    arguments: tuple[str, ...],
    environment: dict[str, str],
) -> None:
    """Join containment and arm parent death before replacing this process."""

    if os.getppid() != watchdog_pid:
        raise RuntimeError("watchdog disappeared before exec guard armed")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, _SIGKILL) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    if os.getppid() != watchdog_pid:
        raise RuntimeError("watchdog disappeared while exec guard armed")
    (cgroup / "cgroup.procs").write_text(str(os.getpid()), encoding="ascii")
    started = _with_leased_directory_descriptor(arguments)
    os.execvpe(started[0], started, environment)


def _with_leased_directory_descriptor(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Name the descriptor a sandbox binds the leased working directory from.

    This process already stands in the directory its launcher opened, checked
    and entered, so opening `.` reaches that same directory without resolving a
    name a peer could have replaced meanwhile. The number only exists here,
    which is why the argv arrives carrying a placeholder instead of one.

    Only the placeholder standing behind its own flag is a placeholder: a job's
    argument that spells the same word is the job's, and stays it.
    """

    handovers = [
        index
        for index, argument in enumerate(arguments)
        if argument == LEASED_DIRECTORY_DESCRIPTOR
        and index > 0
        and arguments[index - 1] == LEASED_DIRECTORY_BIND
    ]
    if not handovers:
        return arguments
    descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    os.set_inheritable(descriptor, True)
    named = list(arguments)
    for index in handovers:
        named[index] = str(descriptor)
    return tuple(named)


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atelier2-agent-exec-guard")
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--watchdog-pid", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    command = tuple(parsed.command)
    if command[:1] == ("--",):
        command = command[1:]
    if not command:
        parser.error("a provider command is required")
    encoded_environment = os.environ.pop(_ENVIRONMENT_CHANNEL, None)
    if encoded_environment is None:
        parser.error("the provider environment channel is required")
    environment = {
        str(name): str(value)
        for name, value in json.loads(
            base64.b64decode(encoded_environment, validate=True).decode("utf-8")
        )
    }
    guarded_exec(
        cgroup=parsed.cgroup,
        watchdog_pid=parsed.watchdog_pid,
        arguments=command,
        environment=environment,
    )


if __name__ == "__main__":
    main()
