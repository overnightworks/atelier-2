"""Read one attempt's own workspace, and nothing a provider could reach past it.

`ProviderFilesystemAccess` (`ports.provider_conversations`) is answered here,
beside the lease rather than on it: `AgentAttemptWorkspaceLease` is identity,
never a place that opens its own files. The fence is descriptor-anchored, not
path-anchored -- `entered_leased_directory` (`adapters.leased_directory`) holds
the leased directory open by its checked device and inode, and the requested
path is resolved relative to that one held descriptor in a single kernel call.
Nothing here calls `realpath`: resolving the leased name a second time is
exactly the race `entered_leased_directory` exists to close.

A userspace walk that opens one component at a time (`O_NOFOLLOW`, checking
`st_dev` after every step) cannot see a same-filesystem bind mount, and leaves
a window between its own steps for a component to be swapped. `openat2(2)`
(Linux 5.6+) resolves the whole relative path in the kernel in one syscall,
fenced by its own `resolve` mask: `RESOLVE_BENEATH` refuses `..`/absolute
escapes, `RESOLVE_NO_SYMLINKS` refuses every symlink component,
`RESOLVE_NO_XDEV` refuses crossing any mount -- including a bind mount on the
same filesystem, which a `st_dev` comparison on our own descriptors could
never see (`man 2 openat2`) -- and `RESOLVE_NO_MAGICLINKS` refuses procfs-style
magic links. No wrapper for it exists in the standard library, so it is called
through `ctypes`, the same way `adapters.runner_child` calls Landlock.

The resolved descriptor is opened `O_PATH`: no device's own open routine runs,
so a FIFO or a device node can neither block this call nor misbehave before
its type is known. Its `fstat` decides everything before any data is ever
touched -- not a regular file, hard-linked (`st_nlink > 1`, which no
`RESOLVE_*` flag addresses since a hard link is neither a symlink nor a mount),
or already past the read ceiling is refused right there, before a single byte
is read. Only a confirmed regular, singly-linked, bounded file is promoted to
a readable descriptor, and only through `/proc/self/fd/<n>` -- the documented
way to obtain data access to an already-resolved `O_PATH` descriptor's own
inode without any further name lookup.

A request is refused, never raised past `answer`: every reachable failure --
an escape, a path no filesystem encoding could ever hold, a symlink, a mount
boundary, a hard link, a non-regular file, a lease or a resolved file
changing identity underneath this call, a file wider than the injected
ceiling, a write naming a protected path, a write whose parent directory does
not exist, a write onto a name that is itself a symlink, or an unclassified
I/O fault -- is a typed member of `AttemptWorkspaceFileRefusal`, carried on
the outcome `describe` returns.

A write is staged, never opened onto its final name directly. Its content is
checked against the same ceiling before anything is created; its parent
directory is resolved through the identical `openat2` fence a read uses, so a
component swap or an escape is caught the same way; the bytes land in a
freshly created, exclusively named sibling in that same directory; and only a
successful `fsync` followed by a directory-entry `replace` ever makes them
visible under the requested name. A name that already stands there as a
symlink is refused before any sibling is even created, and a name naming a
protected entry -- an SSH identity directory, a shell startup file, or any
Git-managed name -- is refused lexically, before any resolution is attempted
at all: a provider that could plant its own key, startup script, or redirect
Git's own trust would run code the next login, shell, or Git operation
trusted implicitly.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from atelier2.adapters.leased_directory import (
    LeasedDirectoryChanged,
    entered_leased_directory,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.provider_conversations import (
    ProviderFilesystemAnswer,
    ProviderFilesystemEffect,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderFilesystemRequestId,
)

_FORBIDDEN_COMPONENTS = ("", ".", "..")

# `struct open_how` (Linux `<linux/openat2.h>`): the ABI is exactly these three
# `u64` fields, in this order, with no padding.
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_FENCED_RESOLUTION = (
    _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_XDEV | _RESOLVE_NO_MAGICLINKS
)
# The syscall number is an ABI fact of the architecture, not of the kernel
# version: naming it wrong would invoke a different syscall outright, so an
# architecture this has not been checked against fails loudly instead of
# guessing.
_SYS_OPENAT2_BY_MACHINE = {"x86_64": 437}


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


def _openat2_syscall_number() -> int:
    machine = platform.machine()
    try:
        return _SYS_OPENAT2_BY_MACHINE[machine]
    except KeyError:
        raise RuntimeError(
            f"the openat2 syscall number is not known for {machine}; add it "
            "before serving the workspace file fence on this architecture"
        ) from None


_SYS_OPENAT2 = _openat2_syscall_number()
_LIBC = ctypes.CDLL(None, use_errno=True)


def _openat2_descriptor(dir_fd: int, relative_path: str, open_flags: int) -> int:
    """The one fenced resolution call beneath both a file and a directory probe.

    One call resolves every component, so there is no window between separate
    opens for a component to be swapped -- the fence `RESOLVE_BENEATH |
    RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV | RESOLVE_NO_MAGICLINKS` is enforced
    by the kernel across the whole path, not reconstructed here one step at a
    time.
    """

    how = _OpenHow(open_flags | os.O_CLOEXEC, 0, _FENCED_RESOLUTION)
    descriptor = _LIBC.syscall(
        _SYS_OPENAT2,
        dir_fd,
        os.fsencode(relative_path),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if descriptor == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return descriptor


def _openat2_path_descriptor(dir_fd: int, relative_path: str) -> int:
    """Resolve `relative_path` beneath `dir_fd`, fenced by the kernel itself.

    Opened `O_PATH`: this is a probe, not a data open, so no device's own
    open routine ever runs.
    """

    return _openat2_descriptor(dir_fd, relative_path, os.O_PATH)


def _openat2_directory_descriptor(dir_fd: int, relative_path: str) -> int:
    """Resolve a write's parent directory the same fenced way.

    `O_DIRECTORY` is added so the kernel itself refuses a parent that is not
    a directory, rather than a later `openat`/`replace` call discovering that
    the hard way.
    """

    return _openat2_descriptor(dir_fd, relative_path, os.O_PATH | os.O_DIRECTORY)


class AttemptWorkspaceFileRefusal(StrEnum):
    """Why one filesystem request inside an attempt's lease was not granted."""

    PATH_LEFT_THE_LEASE = "path-left-the-lease"
    PATH_NOT_ENCODABLE = "path-not-encodable"
    PATH_NAMED_A_SYMLINK = "path-named-a-symlink"
    PATH_CROSSED_A_MOUNT = "path-crossed-a-mount"
    FILE_NOT_FOUND = "file-not-found"
    ACCESS_DENIED = "access-denied"
    FILE_IS_HARD_LINKED = "file-is-hard-linked"
    NOT_A_REGULAR_FILE = "not-a-regular-file"
    LEASED_DIRECTORY_CHANGED = "leased-directory-changed"
    RESOLVED_FILE_CHANGED_IDENTITY = "resolved-file-changed-identity"
    FILE_EXCEEDS_THE_CEILING = "file-exceeds-the-ceiling"
    PROTECTED_PATH = "protected-path"
    PARENT_MISSING = "parent-missing"
    TARGET_IS_SYMLINK = "target-is-symlink"
    WORKSPACE_IO_FAILED = "workspace-io-failed"


# `openat2`'s own errno already names most refusals; `ENOSYS`/`EINVAL` (no
# kernel support) and anything else unclassified are left to the outer I/O
# boundary in `describe` rather than guessed at here.
#
# `ENOENT`/`ENOTDIR` name a plain missing path, and `EACCES` names an
# ordinary permission refusal -- neither is an escape, so neither shares
# `PATH_LEFT_THE_LEASE` with the lexical fence. `RESOLVE_BENEATH` and
# `RESOLVE_NO_XDEV` both report `EXDEV`, but the pre-open lexical fence
# already refuses every `..` and every foreign absolute address before this
# call is ever made, so an `EXDEV` this classifier can actually observe is a
# mount crossing, never a `RESOLVE_BENEATH` escape the lexical fence let
# through -- `PATH_LEFT_THE_LEASE` stays reserved for that fence itself.
_OPENAT2_ERRNO_REFUSALS: dict[int, AttemptWorkspaceFileRefusal] = {
    errno.ELOOP: AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK,
    errno.EXDEV: AttemptWorkspaceFileRefusal.PATH_CROSSED_A_MOUNT,
    errno.EACCES: AttemptWorkspaceFileRefusal.ACCESS_DENIED,
    errno.ENOENT: AttemptWorkspaceFileRefusal.FILE_NOT_FOUND,
    errno.ENOTDIR: AttemptWorkspaceFileRefusal.FILE_NOT_FOUND,
}

# Resolving a write's parent directory reuses the same fenced walk and the
# same errno vocabulary, except a missing or non-directory parent asks a
# different question of a caller than a missing file does, so it keeps its
# own name (`PARENT_MISSING`) rather than sharing `FILE_NOT_FOUND`.
_PARENT_OPENAT2_ERRNO_REFUSALS: dict[int, AttemptWorkspaceFileRefusal] = {
    **_OPENAT2_ERRNO_REFUSALS,
    errno.ENOENT: AttemptWorkspaceFileRefusal.PARENT_MISSING,
    errno.ENOTDIR: AttemptWorkspaceFileRefusal.PARENT_MISSING,
}

# Product-owned names, not the CLI-specific globs a provider's own tooling
# might use: a segment matching one of these, anywhere in a write's relative
# path, is refused before any resolution is even attempted. Every `.git`
# segment is refused wholesale rather than only `.git/hooks`: a gitdir
# redirection file or a rewritten `.git/config` can run a command just as
# reliably as a hook can. Compared casefolded, since a case-insensitive mount
# (FAT/exFAT, or a filesystem with case-insensitive lookup enabled) would
# otherwise let `.SSH` reach the very directory `.ssh` names.
_PROTECTED_SEGMENTS_CASEFOLDED = frozenset(
    name.casefold()
    for name in (
        ".ssh",
        ".bashrc",
        ".profile",
        ".zshrc",
        ".bash_profile",
        ".grok",
        ".claude",
        ".cursor",
        ".git",
    )
)

# Matches `host.terminal_seat`'s staged-replace pattern: an exclusively named
# sibling nobody else could be racing for, private-mode because a provider's
# own file is nobody else's to read. A bounded number of fresh names is tried
# before giving up: `O_EXCL` already refuses a genuine collision, and the
# random suffix makes one vanishingly unlikely, but a name this call never
# created must never be the one a caller cleans up.
_STAGED_WRITE_NAME_BYTES = 8
_STAGED_WRITE_NAME_ATTEMPTS = 8
_STAGED_WRITE_MODE = 0o600
_STAGED_WRITE_FLAGS = (
    os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_WRONLY
)


def _names_a_protected_path(parts: tuple[str, ...]) -> bool:
    """Whether any segment of a write's relative path names a protected entry.

    A provider that could plant an SSH key, rewrite a shell startup file, or
    redirect Git's own trust -- a hook, its config, or a gitdir pointer --
    would run its own code, or somebody else's, the next login, shell, or Git
    operation trusts implicitly.
    """

    return any(part.casefold() in _PROTECTED_SEGMENTS_CASEFOLDED for part in parts)


def _has_unencodable_component(parts: tuple[str, ...]) -> bool:
    """Whether any part cannot round-trip through the filesystem encoding.

    `openat2`'s own `os.fsencode` call would otherwise raise
    `UnicodeEncodeError` for a lone surrogate no real path can ever contain --
    an exception past the `OSError` boundary everything else here is refused
    through.
    """

    for part in parts:
        try:
            os.fsencode(part)
        except UnicodeEncodeError:
            return True
    return False


@dataclass(frozen=True, slots=True)
class AttemptWorkspaceFileOutcome:
    """One request's reply, and the refusal it carries when it was not answered.

    `detail` exists for exactly one refusal: an unclassified `OSError` names
    its errno here so the failure stays diagnosable, never the host path a
    caller must not learn.
    """

    reply: ProviderFilesystemReply
    refusal: AttemptWorkspaceFileRefusal | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        answered = self.reply.answer is ProviderFilesystemAnswer.ANSWERED
        if answered and self.refusal is not None:
            raise ValueError("an answered file request carries no refusal")
        if not answered and self.refusal is None:
            raise ValueError("a refused file request names why")
        if (
            self.detail
            and self.refusal is not AttemptWorkspaceFileRefusal.WORKSPACE_IO_FAILED
        ):
            raise ValueError("only a workspace I/O failure names its errno")


class AttemptWorkspaceFileAccess:
    """The one place a running provider's file requests reach real disk.

    Bound to exactly one attempt's lease and to the largest file this
    conversation's reply can afford. Given the same lease, it grants exactly
    what that attempt's own workspace holds -- symlinks, mount boundaries,
    hard links, escapes and a lease whose directory was swapped underneath it
    are refused the same way an oversize file is: as data on the outcome,
    never as an exception.
    """

    def __init__(
        self, lease: AgentAttemptWorkspaceLease, maximum_read_bytes: int
    ) -> None:
        if not isinstance(lease, AgentAttemptWorkspaceLease):
            raise TypeError("attempt workspace file access needs a typed lease")
        if type(maximum_read_bytes) is not int or maximum_read_bytes < 1:
            raise ValueError(
                "attempt workspace file access needs a positive read ceiling"
            )
        if maximum_read_bytes > MAXIMUM_ARTIFACT_BYTES:
            raise ValueError(
                "attempt workspace file access read ceiling exceeds "
                f"{MAXIMUM_ARTIFACT_BYTES} bytes"
            )
        self._lease = lease
        self._maximum_read_bytes = maximum_read_bytes

    def answer(self, request: ProviderFilesystemRequest) -> ProviderFilesystemReply:
        """Do exactly this to exactly that file, or refuse it."""

        return self.describe(request).reply

    def describe(
        self, request: ProviderFilesystemRequest
    ) -> AttemptWorkspaceFileOutcome:
        """The full outcome of one request, refusal included, for a caller
        that wants to know why -- `answer` keeps only what the port promises.
        """

        parts = _leased_relative_parts(request.path, self._lease.working_directory)
        if parts is None:
            return _refused(
                request.request_id, AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE
            )
        if _has_unencodable_component(parts):
            return _refused(
                request.request_id, AttemptWorkspaceFileRefusal.PATH_NOT_ENCODABLE
            )
        if request.effect is ProviderFilesystemEffect.WRITE:
            if _names_a_protected_path(parts):
                return _refused(
                    request.request_id, AttemptWorkspaceFileRefusal.PROTECTED_PATH
                )
            if len(request.content) > self._maximum_read_bytes:
                return _refused(
                    request.request_id,
                    AttemptWorkspaceFileRefusal.FILE_EXCEEDS_THE_CEILING,
                )
        try:
            with entered_leased_directory(
                self._lease.working_directory, self._lease.device, self._lease.inode
            ) as (_entry, root_fd):
                if request.effect is ProviderFilesystemEffect.WRITE:
                    return self._write_within(
                        request.request_id, root_fd, parts, request.content
                    )
                return self._read_within(request.request_id, root_fd, parts)
        except LeasedDirectoryChanged:
            return _refused(
                request.request_id, AttemptWorkspaceFileRefusal.LEASED_DIRECTORY_CHANGED
            )
        except OSError as error:
            return _workspace_io_failure(request.request_id, error)

    def _read_within(
        self,
        request_id: ProviderFilesystemRequestId,
        root_fd: int,
        parts: tuple[str, ...],
    ) -> AttemptWorkspaceFileOutcome:
        try:
            path_fd = _openat2_path_descriptor(root_fd, "/".join(parts))
        except OSError as error:
            refusal = (
                _OPENAT2_ERRNO_REFUSALS.get(error.errno)
                if error.errno is not None
                else None
            )
            if refusal is None:
                raise
            return _refused(request_id, refusal)
        try:
            probed = os.fstat(path_fd)
            if not stat.S_ISREG(probed.st_mode):
                return _refused(
                    request_id, AttemptWorkspaceFileRefusal.NOT_A_REGULAR_FILE
                )
            if probed.st_nlink > 1:
                return _refused(
                    request_id, AttemptWorkspaceFileRefusal.FILE_IS_HARD_LINKED
                )
            if probed.st_size > self._maximum_read_bytes:
                return _refused(
                    request_id, AttemptWorkspaceFileRefusal.FILE_EXCEEDS_THE_CEILING
                )
            data_fd = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY | os.O_CLOEXEC)
        finally:
            os.close(path_fd)
        try:
            resolved = os.fstat(data_fd)
            if (resolved.st_dev, resolved.st_ino) != (probed.st_dev, probed.st_ino):
                return _refused(
                    request_id,
                    AttemptWorkspaceFileRefusal.RESOLVED_FILE_CHANGED_IDENTITY,
                )
            content = _bounded_read(data_fd, self._maximum_read_bytes)
            if content is None:
                return _refused(
                    request_id, AttemptWorkspaceFileRefusal.FILE_EXCEEDS_THE_CEILING
                )
            return AttemptWorkspaceFileOutcome(
                ProviderFilesystemReply(
                    request_id, ProviderFilesystemAnswer.ANSWERED, content
                )
            )
        finally:
            os.close(data_fd)

    def _write_within(
        self,
        request_id: ProviderFilesystemRequestId,
        root_fd: int,
        parts: tuple[str, ...],
        content: bytes,
    ) -> AttemptWorkspaceFileOutcome:
        parent_parts, final_name = parts[:-1], parts[-1]
        if not parent_parts:
            return self._stage_write(request_id, root_fd, final_name, content)
        try:
            parent_fd = _openat2_directory_descriptor(root_fd, "/".join(parent_parts))
        except OSError as error:
            refusal = (
                _PARENT_OPENAT2_ERRNO_REFUSALS.get(error.errno)
                if error.errno is not None
                else None
            )
            if refusal is None:
                raise
            return _refused(request_id, refusal)
        try:
            return self._stage_write(request_id, parent_fd, final_name, content)
        finally:
            os.close(parent_fd)

    def _stage_write(
        self,
        request_id: ProviderFilesystemRequestId,
        parent_fd: int,
        final_name: str,
        content: bytes,
    ) -> AttemptWorkspaceFileOutcome:
        """Write `content` beside `final_name` and move it on, or leave nothing durable.

        `os.replace` is a `rename(2)`, which never follows a symlink at its
        destination -- it replaces the directory entry itself -- so even a
        swap landed between the `lstat` check below and this call still
        lands on the name, never on what it used to point at. A crash
        between creating the staged sibling and replacing `final_name` with
        it is the one failure this call cannot observe or clean up after;
        the sibling it leaves behind is removed not by a later write but by
        `LocalAgentAttemptWorkspaceOwner.release` (`adapters.agent_workspaces`),
        which deletes an attempt's entire workspace tree once its lease ends.
        """

        try:
            target = os.lstat(final_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(target.st_mode):
                return _refused(
                    request_id, AttemptWorkspaceFileRefusal.TARGET_IS_SYMLINK
                )

        try:
            staged_name, descriptor = _create_staged_sibling(parent_fd, final_name)
        except OSError as error:
            return _workspace_io_failure(request_id, error)

        staged_exists = True
        try:
            try:
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                staged_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
            staged_exists = False
        except OSError as error:
            return _workspace_io_failure(request_id, error)
        finally:
            if staged_exists:
                with suppress(FileNotFoundError):
                    os.unlink(staged_name, dir_fd=parent_fd)
        return AttemptWorkspaceFileOutcome(
            ProviderFilesystemReply(request_id, ProviderFilesystemAnswer.ANSWERED)
        )


def _create_staged_sibling(parent_fd: int, final_name: str) -> tuple[str, int]:
    """A freshly created, exclusively named sibling of `final_name`.

    Only a name this attempt actually returns as created is ever the caller's
    to clean up: a collision on one random name (`OSError` from `O_EXCL`)
    tries a fresh one instead of reporting the name that lost the race as
    though this call had made it.
    """

    attempt_index = 0
    while True:
        attempt_index += 1
        staged_name = f"{final_name}.{secrets.token_hex(_STAGED_WRITE_NAME_BYTES)}"
        try:
            descriptor = os.open(
                staged_name, _STAGED_WRITE_FLAGS, _STAGED_WRITE_MODE, dir_fd=parent_fd
            )
        except FileExistsError:
            if attempt_index >= _STAGED_WRITE_NAME_ATTEMPTS:
                raise
            continue
        return staged_name, descriptor


def _write_all(descriptor: int, content: bytes) -> None:
    """Every byte of `content`, looping past a short write POSIX may return."""

    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _bounded_read(descriptor: int, ceiling: int) -> bytes | None:
    """Every byte this file holds, or `None` once more than `ceiling` arrived.

    A size already past the ceiling is refused before this is ever called;
    this is the guard for what that one check cannot see -- a file that
    grows past the ceiling between the check and the last byte read. A fixed-
    length `read` sized from that earlier `fstat` would otherwise answer a
    silently truncated prefix as though it were the whole file. Reading in a
    loop, bounded by one byte past the ceiling, means the true byte count
    decides the outcome instead.
    """

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, ceiling + 1 - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > ceiling:
            return None


def _errno_name(error: OSError) -> str:
    if error.errno is None:
        return type(error).__name__
    return errno.errorcode.get(error.errno, str(error.errno))


def _leased_relative_parts(
    requested: Path, working_directory: Path
) -> tuple[str, ...] | None:
    """The requested path's parts inside the lease, or `None` when it escapes.

    An absolute request is accepted only when its leading parts equal the
    attested `working_directory` exactly -- a part-by-part comparison, never a
    string prefix, so `/scratch/attempt-12` never matches a request naming
    `/scratch/attempt-123`. Every other absolute address is an escape, decided
    without opening anything.

    `Path.parts` already collapses every `.` and empty component lexically, at
    parse time and regardless of how the `Path` was built -- there is no
    request this port can carry from which one could survive to be read back
    here. `..` and an embedded NUL are not collapsed, so they are refused
    explicitly; `openat2`'s own `RESOLVE_BENEATH` refuses a `..` escape again
    beneath this one, as defense in depth rather than as the primary fence.
    """

    parts = requested.parts
    if requested.is_absolute():
        anchor = working_directory.parts
        if parts[: len(anchor)] != anchor:
            return None
        parts = parts[len(anchor) :]
    if not parts or any(_is_forbidden_component(part) for part in parts):
        return None
    return parts


def _is_forbidden_component(part: str) -> bool:
    return part in _FORBIDDEN_COMPONENTS or "\x00" in part


def _refused(
    request_id: ProviderFilesystemRequestId,
    refusal: AttemptWorkspaceFileRefusal,
    detail: str = "",
) -> AttemptWorkspaceFileOutcome:
    return AttemptWorkspaceFileOutcome(
        ProviderFilesystemReply(request_id, ProviderFilesystemAnswer.REFUSED),
        refusal,
        detail,
    )


def _workspace_io_failure(
    request_id: ProviderFilesystemRequestId, error: OSError
) -> AttemptWorkspaceFileOutcome:
    return _refused(
        request_id, AttemptWorkspaceFileRefusal.WORKSPACE_IO_FAILED, _errno_name(error)
    )
