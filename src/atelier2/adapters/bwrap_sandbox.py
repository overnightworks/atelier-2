"""Hold one provider child inside its grant, using the host's bubblewrap.

The fence is a pure argv transformation: a grant plus the command a provider
asked for become the command this host really starts. It is a function and not
a launcher because the live start and the start probes that attest it have to
be the same vector -- a probe that attests an unfenced start says nothing about
the fenced start that then runs. `entered_fence` is the one way in: every seam
that starts a provider -- supervision, both composition probes, model
validation -- enters its directory through it and hands it over the same way.

ADR 0009 §1 owns the containment doctrine, and §2 forbids an isolation
mechanism of our own making, which is why nothing here implements a boundary:
bubblewrap does, and this module only says what it may open.

Inside the fence the root filesystem is bubblewrap's own empty tmpfs. The grant
is therefore the whole of what exists: the child's home, its toolchain, the
system files a program needs to run at all, and the directory it stands in.
The operator's keys, the live store and every other checkout are not absent by
a rule that could be worded around -- they have no name in that namespace.

Two things a fenced start must never hand its child, both measured against
bubblewrap 0.9.0: an open descriptor on a host directory, because `openat` on
one walks straight out of every grant -- the enforcer closes the descriptor it
binds from before it runs the command, so a start passes that one and nothing
else -- and a shell, because this transformation prepends words to an argument
vector and never builds a line.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from atelier2.adapters.bounded_processes import (
    BoundedProcessFailure,
    bounded_process_streams,
)
from atelier2.adapters.leased_directory import entered_leased_directory
from atelier2.contracts.sandbox_grants import (
    SandboxedLaunch,
    SandboxGrants,
    SandboxUnavailable,
)

SANDBOX_EXECUTABLE_NAME = "bwrap"

_HOST_PROBE_TIMEOUT_SECONDS = 20.0
_PROBE_OUTPUT_BYTES = 4_096
"""What a probe may write before this seam stops reading it: one marker file and
whatever an enforcer says when it refuses, and nothing that could fill memory."""
_PROBE_PREFIX = "atelier2-fence-probe-"
_PROBE_MARKER_NAME = "grant"
_PROBE_MARKER_TEXT = "one directory, handed over as its descriptor\n"
_PROBE_READER = Path("/bin/cat")
"""What the host probe runs behind the fence: coreutils reading one file.

The probe needs a command that proves the bind really happened rather than one
that merely started, and it reads its answer out of the same system roots every
fenced child is granted.
"""

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
_WRITABLE_BIND_OF_DESCRIPTOR = "--bind-fd"
_ENTER_DIRECTORY = "--chdir"
_END_OF_FLAGS = "--"

_PROCESS_TABLE_PATH = Path("/proc")
_DEVICE_PATH = Path("/dev")
_TEMPORARY_PATH = Path("/tmp")

SYSTEM_READ_ONLY_ROOTS = (
    Path("/usr"),
    Path("/bin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/sbin"),
)
"""What any program on this host needs before it is a program at all.

`/usr` carries the binaries and shared libraries, and the four names beside it
are where a merged-usr system keeps the loader and the shell a tool spawns.
They are read only: a child that may rewrite the tools it runs is not fenced by
them. A whole `/etc` is not among them -- that would hand a child every
account, service and credential file this host configures.
"""

SYSTEM_READ_ONLY_FILES = (
    Path("/etc/resolv.conf"),
    Path("/etc/hosts"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/ssl/certs"),
    Path("/etc/ssl/openssl.cnf"),
    Path("/etc/ca-certificates"),
    Path("/etc/ca-certificates.conf"),
    Path("/etc/passwd"),
    Path("/etc/group"),
    Path("/etc/localtime"),
)
"""The named parts of `/etc` a tool that speaks HTTPS needs, and no more.

The resolver's three files answer a hostname, the public certificate material
answers whether that answer may be trusted, the account files let a runtime
name the user it runs as, and the zone file makes its timestamps this host's.

`/etc/ssl` is not granted whole: the same directory that holds the public
certificate store holds `/etc/ssl/private`, the keys a server on this account
can read. Only what a client needs to verify a certificate is named here.
Anything else a real toolchain turns out to need is a named gap on the item
that owns this fence, never a widening nobody wrote down.
"""


def sandboxed_arguments(
    arguments: tuple[str, ...],
    working_directory: Path,
    launch: SandboxedLaunch | None,
    working_directory_descriptor: int,
) -> tuple[str, ...]:
    """The argv one start really runs: the command, held inside its grant.

    A command that declared no grant is returned as it came, because a vector
    without a fence is a decision its executor states, not one this function
    may invent.

    The working directory is always the child's to write in -- it is where the
    provider was told to work -- and it is always bound from the descriptor its
    launcher opened and checked. No start of this repository binds it by name:
    a name is what something else can stand in for between the check and the
    mount.
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
        if path != working_directory:
            fenced += [_WRITABLE_BIND, str(path), str(path)]
    fenced += [
        _WRITABLE_BIND_OF_DESCRIPTOR,
        str(working_directory_descriptor),
        str(working_directory),
        _ENTER_DIRECTORY,
        str(working_directory),
        _END_OF_FLAGS,
    ]
    return (*fenced, *arguments)


@contextmanager
def entered_fence(
    arguments: tuple[str, ...],
    working_directory: Path,
    device: int,
    inode: int,
    launch: SandboxedLaunch | None,
) -> Iterator[tuple[tuple[str, ...], str, tuple[int, ...]]]:
    """What one fenced start is made of: its argv, its `cwd`, its descriptors.

    The directory is opened once and checked against the identity it was leased
    under, and that one descriptor is both what the child enters through and
    what the enforcer binds from -- so nothing between the check and the mount
    can swap the directory, and no second name for it is ever resolved.

    The descriptors yielded here are the whole set a start may pass on. The
    enforcer closes the one it binds from before it runs the command, so a
    fenced child holds no descriptor of this host at all; one passed beside it
    would be a door out of every grant, because `openat` on a directory
    descriptor answers `..` as readily as any other name.
    """

    with entered_leased_directory(working_directory, device, inode) as (
        entered,
        descriptor,
    ):
        yield (
            sandboxed_arguments(arguments, working_directory, launch, descriptor),
            entered,
            (descriptor,),
        )


def sandbox_frame(launch: SandboxedLaunch | None) -> dict[str, object] | None:
    """One grant as a launch frame carries it to the process that enforces it."""

    if launch is None:
        return None
    return {
        "enforcer": str(launch.enforcer),
        "readable_and_executable": [
            str(path) for path in launch.grants.readable_and_executable
        ],
        "writable": [str(path) for path in launch.grants.writable],
    }


def sandbox_from_frame(value: object) -> SandboxedLaunch | None:
    """The grant a launch frame declared, or a refusal to read it as one.

    This is the one place a boundary arrives from outside the process that
    draws it, so its shape is checked rather than trusted: a grant this reader
    cannot recognise ends the launch instead of widening it.
    """

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "enforcer",
        "readable_and_executable",
        "writable",
    }:
        raise ValueError("launch sandbox is malformed")
    enforcer = value["enforcer"]
    if type(enforcer) is not str:
        raise ValueError("launch sandbox enforcer is malformed")
    return SandboxedLaunch(
        Path(enforcer),
        SandboxGrants(
            _framed_paths(value["writable"]),
            _framed_paths(value["readable_and_executable"]),
        ),
    )


def _framed_paths(value: object) -> tuple[Path, ...]:
    if not isinstance(value, list) or any(
        not isinstance(entry, str) for entry in value
    ):
        raise ValueError("launch sandbox grant is malformed")
    return tuple(Path(entry) for entry in value)


def toolchain_sandbox(
    executable: Path, enforcer: Path, state_directory: Path
) -> SandboxedLaunch:
    """Grant one command-line toolchain its own files, and nothing beside them.

    The grant is named here rather than derived from the deployment's search
    path: a path entry is what a shell looks through, and turning that into
    reach hands a child whatever happens to stand on it -- a home directory, a
    checkout, a scratch root. So a fenced child reads its own executable and
    the system roots any program needs, writes the private state directory it
    was given, and works in the directory it was leased.

    What a real toolchain needs beyond that is a named gap on the item that
    owns this fence rather than a widening: the release this vector serves is
    one statically linked executable, and a tool it shells out to that stands
    under no system root is not there.

    The enforcer is the absolute path this deployment was configured with,
    never a name looked up again on a search path, and it is verified on every
    start rather than remembered from composition: one that disappeared or
    stopped working has to stop the next launch, not the next restart.
    """

    verified_sandbox_host(enforcer)
    readable = _narrowed((executable, *SYSTEM_READ_ONLY_ROOTS, *SYSTEM_READ_ONLY_FILES))
    return SandboxedLaunch(enforcer, SandboxGrants((state_directory,), readable))


def resolved_sandbox_executable(search_path: str) -> Path:
    """Where this host keeps its enforcer, asked once when a deployment is composed.

    A name is looked up here and nowhere else: what a launch starts is the
    absolute path this answer became, so no later change to a search path can
    put another binary in the fence's place. A host that carries none answers
    with the bare name, which no start accepts -- an executor that needs a
    fence is refused where its startability is probed, and the house it is one
    executor of still serves.
    """

    found = shutil.which(SANDBOX_EXECUTABLE_NAME, path=search_path)
    return Path(found) if found is not None else Path(SANDBOX_EXECUTABLE_NAME)


def verified_sandbox_host(enforcer: Path) -> None:
    """Refuse an enforcer this host cannot really fence a start with.

    The capability is probed, never read off a version number: `--bind-fd`
    reached different releases through different distributions, so a number is
    a claim about a build while the option is the fact. One throwaway start
    answers all of it at once -- that this binary is here, that this account
    may still open a user namespace, that every option a launch uses parses,
    and that a directory handed over as a descriptor really arrives -- because
    it is that start, composed by the same function a job's is.
    """

    if not enforcer.is_absolute():
        raise SandboxUnavailable(
            f"serving a tool-bearing provider needs {SANDBOX_EXECUTABLE_NAME} at an "
            f"absolute path, and this deployment named {enforcer}: a name is "
            "resolved again wherever a launch stands, which for a fenced start is "
            "a directory a provider writes"
        )
    _attest_enforcer(enforcer)


def _attest_enforcer(enforcer: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=_PROBE_PREFIX) as probe_root:
        directory = Path(probe_root)
        marker = directory / _PROBE_MARKER_NAME
        marker.write_text(_PROBE_MARKER_TEXT, encoding="utf-8")
        standing = directory.stat()
        grants = SandboxGrants(
            readable_and_executable=_narrowed((_PROBE_READER, *SYSTEM_READ_ONLY_ROOTS))
        )
        with entered_fence(
            (str(_PROBE_READER), str(marker)),
            directory,
            standing.st_dev,
            standing.st_ino,
            SandboxedLaunch(enforcer, grants),
        ) as (arguments, entered, inherited):
            answered = _answered(arguments, entered, inherited, enforcer)
    if answered != _PROBE_MARKER_TEXT:
        raise SandboxUnavailable(
            f"{enforcer} did not hand a directory it was given as a descriptor to a "
            f"command behind the fence: that command answered {answered!r}"
        )


def _answered(
    arguments: tuple[str, ...],
    entered: str,
    inherited: tuple[int, ...],
    enforcer: Path,
) -> str:
    """What one probe start wrote, under a byte bound and a deadline of its own."""

    refusal = f"{enforcer} could not start the fence this deployment needs"
    try:
        process = subprocess.Popen(
            arguments,
            cwd=entered,
            pass_fds=inherited,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return_code, answer, diagnostics = bounded_process_streams(
            process, _HOST_PROBE_TIMEOUT_SECONDS, _PROBE_OUTPUT_BYTES
        )
    except (OSError, subprocess.SubprocessError, BoundedProcessFailure) as error:
        raise SandboxUnavailable(f"{refusal}: {error}") from error
    if return_code != 0:
        raise SandboxUnavailable(
            f"{refusal}: {diagnostics.decode('utf-8', 'replace').strip()}"
        )
    return answer.decode("utf-8", "replace")


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
