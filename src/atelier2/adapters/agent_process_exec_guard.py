from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

_PR_SET_PDEATHSIG = 1
_SIGKILL = 9
_ENVIRONMENT_CHANNEL = "ATELIER2_AGENT_ENVIRONMENT_B64"


GUARD_MODULE = "atelier2.adapters.agent_process_exec_guard"


def guarded_arguments(
    cgroup: Path,
    watchdog_pid: int,
    arguments: tuple[str, ...],
    environment: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, ...], dict[str, str]]:
    """How one command is started under this guard: its argv and its environment.

    The guard's own invocation and the channel its child's environment travels
    on are one protocol, so they are composed here rather than restated by the
    supervision that spawns it. The environment a launch declares is complete,
    which is why it travels encoded beside the controller's own rather than as
    an overlay on it.
    """

    guarded = (
        sys.executable,
        # Isolated, because this interpreter starts in the directory a provider
        # writes: without it Python would import this module through whatever
        # `atelier2` package that directory happens to hold, before the guard
        # has joined containment or the fence exists.
        "-I",
        "-m",
        GUARD_MODULE,
        "--cgroup",
        str(cgroup),
        "--watchdog-pid",
        str(watchdog_pid),
        "--",
        *arguments,
    )
    channel = base64.b64encode(
        json.dumps(sorted(environment), separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return guarded, {**os.environ, _ENVIRONMENT_CHANNEL: channel}


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
    os.execvpe(arguments[0], arguments, environment)


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
