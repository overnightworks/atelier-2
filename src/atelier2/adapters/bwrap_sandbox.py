"""Hold one provider child inside its grant, using the host's bubblewrap.

The fence is a pure argv transformation: a grant plus the command a provider
asked for become the command this host really starts. It is a function and not
a launcher because the live start and the start probes that attest it have to
be the same vector -- a probe that attests an unfenced start says nothing about
the fenced start that then runs.

ADR 0009 §1 owns the containment doctrine, and §2 forbids an isolation
mechanism of our own making, which is why nothing here implements a boundary:
bubblewrap does, and this module only says what it may open.

Inside the fence the root filesystem is bubblewrap's own empty tmpfs. The grant
is therefore the whole of what exists: the child's home, its toolchain, the
system files a program needs to run at all, and the directory it stands in.
The operator's keys, the live store and every other checkout are not absent by
a rule that could be worded around -- they have no name in that namespace.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from atelier2.contracts.sandbox_grants import (
    LEASED_DIRECTORY_BIND,
    LEASED_DIRECTORY_DESCRIPTOR,
    SandboxedLaunch,
    SandboxGrants,
    SandboxUnavailable,
)
from atelier2.ports.agent_executions import AgentProcessInvocation

SANDBOX_EXECUTABLE_NAME = "bwrap"
MINIMUM_SANDBOX_VERSION = (0, 9, 0)
"""`--bind-fd` arrived in bubblewrap 0.9.0.

It is the whole reason for a floor: without it a leased directory could only be
handed over as a path, and resolving that name a second time is exactly the
window the lease exists to close.
"""

_VERSION_FLAG = "--version"
_REPORTED_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_HOST_PROBE_TIMEOUT_SECONDS = 20.0

_UNSHARE_EVERY_NAMESPACE = "--unshare-all"
_KEEP_THE_NETWORK = "--share-net"
"""The network stays: a provider child talks to its own API, and this slice
draws the file boundary only. A domain boundary needs an enforcer of its own
(ADR 0011)."""
_DIE_WITH_PARENT = "--die-with-parent"
_PROCESS_TABLE = "--proc"
_DEVICE_FILES = "--dev"
_TEMPORARY_FILESYSTEM = "--tmpfs"
_READ_ONLY_BIND = "--ro-bind"
_WRITABLE_BIND = "--bind"
_ENTER_DIRECTORY = "--chdir"
_END_OF_FLAGS = "--"

_PROCESS_TABLE_PATH = Path("/proc")
_DEVICE_PATH = Path("/dev")
_TEMPORARY_PATH = Path("/tmp")
SYSTEM_READ_ONLY_ROOTS = (
    Path("/usr"),
    Path("/etc"),
    Path("/bin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/sbin"),
)
"""What any program on this host needs before it is a program at all.

`/usr` carries the binaries and shared libraries, `/etc` the certificate store
and the resolver a network call needs, and the four remaining names are where a
merged-usr system keeps the loader and the shell a tool spawns. They are read
only: a child that may rewrite the tools it runs is not fenced by them.
"""


def sandboxed_arguments(
    arguments: tuple[str, ...],
    working_directory: Path,
    launch: SandboxedLaunch | None,
    working_directory_descriptor: str | None = None,
) -> tuple[str, ...]:
    """The argv one start really runs: the command, held inside its grant.

    A command that declared no grant is returned as it came, because a vector
    without a fence is a decision its executor states, not one this function
    may invent.

    The working directory is always the child's to write in -- it is where the
    provider was told to work. It is bound from `working_directory_descriptor`
    where the launcher opened and verified the directory itself, and by path
    only where the caller created the directory it is about to enter.
    """

    if launch is None:
        return arguments
    fenced = [
        str(launch.enforcer),
        _UNSHARE_EVERY_NAMESPACE,
        _KEEP_THE_NETWORK,
        _DIE_WITH_PARENT,
        _PROCESS_TABLE,
        str(_PROCESS_TABLE_PATH),
        _DEVICE_FILES,
        str(_DEVICE_PATH),
        _TEMPORARY_FILESYSTEM,
        str(_TEMPORARY_PATH),
    ]
    for path in launch.grants.readable_and_executable:
        fenced += [_READ_ONLY_BIND, str(path), str(path)]
    for path in launch.grants.writable:
        fenced += [_WRITABLE_BIND, str(path), str(path)]
    if working_directory_descriptor is None:
        if working_directory not in launch.grants.writable:
            fenced += [_WRITABLE_BIND, str(working_directory), str(working_directory)]
    else:
        fenced += [
            LEASED_DIRECTORY_BIND,
            working_directory_descriptor,
            str(working_directory),
        ]
    fenced += [_ENTER_DIRECTORY, str(working_directory), _END_OF_FLAGS]
    return (*fenced, *arguments)


def launch_arguments(invocation: AgentProcessInvocation) -> tuple[str, ...]:
    """The argv this supervised launch starts, fenced where its command asked.

    The leased directory is named by the placeholder the launching process
    resolves, because only that process knows the number of the descriptor it
    opened.
    """

    return sandboxed_arguments(
        invocation.command.arguments,
        invocation.lease.working_directory,
        invocation.command.sandbox,
        LEASED_DIRECTORY_DESCRIPTOR,
    )


def toolchain_sandbox(
    executable: Path, search_path: str, state_directory: Path
) -> SandboxedLaunch:
    """Grant one command-line toolchain its own files, and nothing beside them.

    What the child may run is its own executable plus whatever stands on the
    deployment's search path -- the same programs its shell would find without
    a fence -- read only. What it may write is the private state directory it
    was given. The host is verified on every start rather than remembered from
    composition: an enforcer that disappeared has to stop the next launch, not
    the next restart.
    """

    enforcer = verified_sandbox_host(search_path)
    readable = _narrowed(
        (executable, *SYSTEM_READ_ONLY_ROOTS, *_search_path_directories(search_path))
    )
    return SandboxedLaunch(enforcer, SandboxGrants((state_directory,), readable))


def verified_sandbox_host(search_path: str) -> Path:
    """The enforcer this host offers, or the reason it may not serve fenced work.

    Three questions, because each has its own answer: is bubblewrap on the
    deployment's search path at all, is it a release that can take a directory
    as a descriptor, and does this kernel still let this account open a user
    namespace. The last is asked by fencing bubblewrap's own version call, so
    what is proved is the transformation this module really emits.
    """

    found = shutil.which(SANDBOX_EXECUTABLE_NAME, path=search_path)
    if found is None:
        raise SandboxUnavailable(
            f"serving a tool-bearing provider needs {SANDBOX_EXECUTABLE_NAME} on "
            f"the deployment's search path, and {search_path!r} carries none"
        )
    enforcer = Path(found)
    version = _reported_version(enforcer)
    if version < MINIMUM_SANDBOX_VERSION:
        raise SandboxUnavailable(
            f"{enforcer} reports version {_spelled(version)}, and a leased "
            "directory can only be handed over as a descriptor from "
            f"{_spelled(MINIMUM_SANDBOX_VERSION)} on"
        )
    _attest_user_namespace(enforcer)
    return enforcer


def _spelled(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _reported_version(enforcer: Path) -> tuple[int, int, int]:
    answer = _answered(
        (str(enforcer), _VERSION_FLAG),
        f"{enforcer} did not answer {_VERSION_FLAG}",
    )
    reported = _REPORTED_VERSION.search(answer)
    if reported is None:
        raise SandboxUnavailable(
            f"{enforcer} did not report a version at {_VERSION_FLAG}: {answer.strip()}"
        )
    first, second, third = reported.groups()
    return int(first), int(second), int(third)


def _attest_user_namespace(enforcer: Path) -> None:
    """Start the enforcer's own version call inside the fence it composes."""

    grants = SandboxGrants(
        readable_and_executable=_narrowed((enforcer, *SYSTEM_READ_ONLY_ROOTS))
    )
    _answered(
        sandboxed_arguments(
            (str(enforcer), _VERSION_FLAG),
            SYSTEM_READ_ONLY_ROOTS[0],
            SandboxedLaunch(enforcer, grants),
        ),
        f"{enforcer} could not open a user namespace for this account",
    )


def _answered(arguments: tuple[str, ...], refusal: str) -> str:
    try:
        answer = subprocess.run(
            arguments,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=_HOST_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxUnavailable(f"{refusal}: {error}") from error
    if answer.returncode != 0:
        raise SandboxUnavailable(f"{refusal}: {answer.stderr.strip()}")
    return answer.stdout


def _search_path_directories(search_path: str) -> tuple[Path, ...]:
    """Every directory this deployment's search path really offers a child."""

    return tuple(
        Path(entry)
        for entry in dict.fromkeys(search_path.split(os.pathsep))
        if entry and Path(entry).is_absolute() and Path(entry).is_dir()
    )


def _narrowed(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """The same grant with nothing named twice and nothing named twice over.

    A path that does not exist is nothing to grant, and bubblewrap refuses a
    whole start over one; a path already inside another granted path adds no
    reach and only makes the record harder to read.
    """

    standing = tuple(dict.fromkeys(path for path in paths if path.exists()))
    return tuple(
        path
        for path in standing
        if not any(other in path.parents for other in standing)
    )
