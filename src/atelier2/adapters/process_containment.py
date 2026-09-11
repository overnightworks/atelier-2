"""How supervision signals the processes one launch holds.

A launch is held by a kill cgroup: whatever it starts joins it, however deep
its own process tree goes, and one write ends all of it. Asking is the other
half, and it is not one call for every launch, because a fenced launch has a
first process that is not the provider.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import subprocess
from functools import cache
from pathlib import Path
from typing import NamedTuple

CGROUP_V2_MOUNT = Path("/sys/fs/cgroup")
"""Where a host that supervises this way mounts the one cgroup v2 hierarchy."""


class _PidfdSyscalls(NamedTuple):
    open_by_pid: int
    send_signal: int


# The syscall numbers are an ABI fact of the architecture, not of the kernel
# version: naming one wrong would invoke a different syscall outright, so an
# architecture this has not been checked against fails loudly instead of
# guessing. CPython wraps both calls only where its own build machine's
# headers carried them, and the interpreter this project pins carries neither
# `os.pidfd_open` nor `signal.pidfd_send_signal`, so they are called through
# `ctypes`, the same way `adapters.attempt_workspace_files` calls `openat2`.
_PIDFD_SYSCALLS_BY_MACHINE = {
    "x86_64": _PidfdSyscalls(open_by_pid=434, send_signal=424)
}


@cache
def _pidfd_syscalls() -> _PidfdSyscalls:
    """The numbers this architecture answers the two pidfd calls on.

    Asked where a pidfd is first needed rather than while this module loads,
    because only a fenced launch signals through one: an architecture nobody
    has checked still supervises its unfenced launches through `killpg`.
    """

    machine = platform.machine()
    try:
        return _PIDFD_SYSCALLS_BY_MACHINE[machine]
    except KeyError:
        raise RuntimeError(
            f"the pidfd syscall numbers are not known for {machine}; add them "
            "before supervising a fenced launch on this architecture"
        ) from None


_LIBC = ctypes.CDLL(None, use_errno=True)


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

    membership = _membership_line(cgroup)
    asked = False
    for member in cgroup_members(cgroup):
        if member == enforcer_pid:
            continue
        if _ask_one_member(member, membership, signal_number):
            asked = True
    return asked


def _ask_one_member(pid: int, membership: bytes, signal_number: int) -> bool:
    """Ask the process this number named when it was listed, and nothing else.

    A member that exits and is reaped between the listing and the signal frees
    its number for any process on this host, so the descriptor is taken first
    and everything after it is decided about that one pinned process: a number
    whose current holder stands outside this cgroup was reused, and the pinned
    process behind the descriptor is already gone, so neither is signalled.
    """

    descriptor = _pinned_pidfd(pid)
    if descriptor is None:
        return False
    try:
        if not _stands_inside(pid, membership):
            return False
        return _asked_through_pidfd(descriptor, signal_number)
    finally:
        os.close(descriptor)


def _membership_line(cgroup: Path) -> bytes:
    """The `/proc/<pid>/cgroup` line every process inside `cgroup` carries."""

    return b"0::" + os.fsencode(f"/{cgroup.relative_to(CGROUP_V2_MOUNT)}")


def _stands_inside(pid: int, membership: bytes) -> bool:
    try:
        listed = Path(f"/proc/{pid}/cgroup").read_bytes()
    except OSError:
        return False
    return membership in listed.splitlines()


def _pinned_pidfd(pid: int) -> int | None:
    """A descriptor pinning what this number names now, or `None` once gone."""

    descriptor = _LIBC.syscall(_pidfd_syscalls().open_by_pid, pid, 0)
    if descriptor != -1:
        return descriptor
    code = ctypes.get_errno()
    if code == errno.ESRCH:
        return None
    raise OSError(code, os.strerror(code))


def _asked_through_pidfd(descriptor: int, signal_number: int) -> bool:
    sent = _LIBC.syscall(
        _pidfd_syscalls().send_signal, descriptor, signal_number, None, 0
    )
    if sent == 0:
        return True
    code = ctypes.get_errno()
    if code == errno.ESRCH:
        return False
    raise OSError(code, os.strerror(code))
