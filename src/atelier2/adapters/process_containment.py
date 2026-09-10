"""How supervision signals the processes one launch holds.

A launch is held by a kill cgroup: whatever it starts joins it, however deep
its own process tree goes, and one write ends all of it. Asking is the other
half, and it is not one call for every launch, because a fenced launch has a
first process that is not the provider.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def cgroup_populated(cgroup: Path) -> bool:
    events = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
    return "populated 1" in events


def cgroup_members(cgroup: Path) -> tuple[int, ...]:
    return tuple(
        int(line)
        for line in (cgroup / "cgroup.procs").read_text(encoding="ascii").split()
    )


def killpg(process: subprocess.Popen[bytes], signal_number: int) -> bool:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        return False
    return True


def ask_provider_to_end(
    process: subprocess.Popen[bytes],
    cgroup: Path,
    enforcer_pid: int | None,
    signal_number: int,
) -> bool:
    """Ask a launch's provider to end, without ending the fence it runs in.

    An unfenced launch is its own provider, and its process group is every
    process it started; a fenced one is not, so it is asked through the cgroup
    that holds them all.
    """

    if enforcer_pid is None:
        return killpg(process, signal_number)
    return _ask_everything_but_the_enforcer(cgroup, enforcer_pid, signal_number)


def _ask_everything_but_the_enforcer(
    cgroup: Path, enforcer_pid: int, signal_number: int
) -> bool:
    """Ask everything this launch holds except the enforcer to end.

    Measured on bubblewrap 0.9.0: the enforcer is the first process of the
    namespace it opened, so signalling it tears that namespace down and the
    kernel kills whatever was still finishing its answer -- a provider given a
    second to write its last words never got it, whether the signal went to the
    process group or to the enforcer alone. Signalled this way instead, the
    same provider finished, wrote its evidence and exited, and the enforcer
    ended by itself once nothing was left inside it.

    Every process of the cgroup is asked rather than only the enforcer's own
    child, because a provider that spawned a shell has put more than one
    process in there and a launch ends when they all do.
    """

    asked = False
    for member in cgroup_members(cgroup):
        if member == enforcer_pid:
            continue
        try:
            os.kill(member, signal_number)
        except ProcessLookupError:
            continue
        asked = True
    return asked
