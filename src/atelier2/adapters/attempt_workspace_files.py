"""Read one attempt's own workspace, and nothing a provider could reach past it.

`ProviderFilesystemAccess` (`ports.provider_conversations`) is answered here,
beside the lease rather than on it: `AgentAttemptWorkspaceLease` is identity,
never a place that opens its own files. The fence is descriptor-anchored, not
path-anchored -- `entered_leased_directory` (`adapters.leased_directory`) holds
the leased directory open by its checked device and inode, and every further
step walks one relative component at a time with `os.open(..., dir_fd=...,
O_NOFOLLOW)`, closing the previous step's descriptor once the next one is
open. Nothing here calls `realpath`: resolving the leased name a second time
is exactly the race `entered_leased_directory` exists to close, and resolving
a requested path the same way would reopen it one level down.

`O_NOFOLLOW` alone stops a symlink, not a mount or a hard link: a bind mount
grafted under the leased tree would still be reached by a descriptor whose
`st_dev` differs from the lease's own, and a hard link planted inside the
workspace names a file outside it without ever being a symlink. Both are
checked by identity on every opened descriptor, never by path.

A request is refused, never raised past `answer`: every reachable failure --
an escaping path, a symlink or a mount boundary or a hard link anywhere in it,
the leased directory having changed identity underneath this call, a file
that is not a regular file or wider than the injected ceiling, an
unclassified I/O fault, or a write, which this slice grants nobody -- is a
typed member of `AttemptWorkspaceFileRefusal`, carried on the outcome
`describe` returns.
"""

from __future__ import annotations

import errno
import os
import stat
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

_INTERMEDIATE_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
# `O_NONBLOCK` on the final entry is what keeps a FIFO from hanging this call
# before its type is even known: a regular file ignores the flag, and every
# other type is refused by the `S_ISREG` check right after the open.
_FINAL_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_FORBIDDEN_COMPONENTS = ("", ".", "..")
# A path this deep is not a workspace file a provider legitimately names; the
# bound also keeps one request from holding open descriptors without limit.
_MAXIMUM_WORKSPACE_PATH_COMPONENTS = 64


class AttemptWorkspaceFileRefusal(StrEnum):
    """Why one filesystem request inside an attempt's lease was not granted."""

    PATH_LEFT_THE_LEASE = "path-left-the-lease"
    PATH_NAMED_A_SYMLINK = "path-named-a-symlink"
    PATH_CROSSED_A_MOUNT = "path-crossed-a-mount"
    FILE_IS_HARD_LINKED = "file-is-hard-linked"
    NOT_A_REGULAR_FILE = "not-a-regular-file"
    LEASED_DIRECTORY_CHANGED = "leased-directory-changed"
    FILE_EXCEEDS_THE_CEILING = "file-exceeds-the-ceiling"
    WRITE_NOT_GRANTED = "write-not-granted"
    WORKSPACE_IO_FAILED = "workspace-io-failed"


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

        if request.effect is ProviderFilesystemEffect.WRITE:
            return _refused(
                request.request_id, AttemptWorkspaceFileRefusal.WRITE_NOT_GRANTED
            )
        parts = _leased_relative_parts(request.path, self._lease.working_directory)
        if parts is None:
            return _refused(
                request.request_id, AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE
            )
        try:
            with entered_leased_directory(
                self._lease.working_directory, self._lease.device, self._lease.inode
            ) as (_entry, root_fd):
                return self._read_within(request.request_id, root_fd, parts)
        except LeasedDirectoryChanged:
            return _refused(
                request.request_id, AttemptWorkspaceFileRefusal.LEASED_DIRECTORY_CHANGED
            )
        except OSError as error:
            return _refused(
                request.request_id,
                AttemptWorkspaceFileRefusal.WORKSPACE_IO_FAILED,
                _errno_name(error),
            )

    def _read_within(
        self,
        request_id: ProviderFilesystemRequestId,
        root_fd: int,
        parts: tuple[str, ...],
    ) -> AttemptWorkspaceFileOutcome:
        owned_fd: int | None = None
        parent_fd = root_fd
        try:
            last_index = len(parts) - 1
            for index, part in enumerate(parts):
                flags = (
                    _FINAL_FILE_FLAGS
                    if index == last_index
                    else _INTERMEDIATE_DIRECTORY_FLAGS
                )
                try:
                    opened = os.open(part, flags, dir_fd=parent_fd)
                except OSError as error:
                    return _refused(
                        request_id, _refusal_for_open_failure(part, parent_fd, error)
                    )
                if owned_fd is not None:
                    os.close(owned_fd)
                owned_fd = opened
                parent_fd = opened
                status = os.fstat(owned_fd)
                if status.st_dev != self._lease.device:
                    return _refused(
                        request_id, AttemptWorkspaceFileRefusal.PATH_CROSSED_A_MOUNT
                    )
                if index == last_index:
                    return self._answered_from(request_id, owned_fd, status)
            raise AssertionError(
                "a nonempty path always answers from its own final component"
            )
        finally:
            if owned_fd is not None:
                os.close(owned_fd)

    def _answered_from(
        self, request_id: ProviderFilesystemRequestId, fd: int, status: os.stat_result
    ) -> AttemptWorkspaceFileOutcome:
        if not stat.S_ISREG(status.st_mode):
            return _refused(request_id, AttemptWorkspaceFileRefusal.NOT_A_REGULAR_FILE)
        if status.st_nlink > 1:
            return _refused(request_id, AttemptWorkspaceFileRefusal.FILE_IS_HARD_LINKED)
        content = _bounded_read(fd, self._maximum_read_bytes)
        if content is None:
            return _refused(
                request_id, AttemptWorkspaceFileRefusal.FILE_EXCEEDS_THE_CEILING
            )
        return AttemptWorkspaceFileOutcome(
            ProviderFilesystemReply(
                request_id, ProviderFilesystemAnswer.ANSWERED, content
            )
        )


def _bounded_read(descriptor: int, ceiling: int) -> bytes | None:
    """Every byte this file holds, or `None` once more than `ceiling` arrived.

    Never trusts a size read before this call: a file that grows between an
    `fstat` and a fixed-length `read` would otherwise answer a silently
    truncated prefix as though it were the whole file. Reading in a loop,
    bounded by one byte past the ceiling, means the true byte count decides
    the outcome instead.
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


def _refusal_for_open_failure(
    part: str, parent_fd: int, error: OSError
) -> AttemptWorkspaceFileRefusal:
    """Why one component's `openat` failed, told apart by asking what it is.

    `O_NOFOLLOW` reports a symlink as `ELOOP` on the final component but as
    `ENOTDIR` on an intermediate one -- the same errno a plain file used where
    a directory was expected would raise. So the failing name is read back
    with `lstat`, which never follows it either, rather than trusted to guess
    from errno alone.
    """

    if error.errno == errno.ELOOP:
        return AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK
    try:
        component = os.lstat(part, dir_fd=parent_fd)
    except OSError:
        return AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE
    if stat.S_ISLNK(component.st_mode):
        return AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK
    return AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE


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
    explicitly.
    """

    parts = requested.parts
    if requested.is_absolute():
        anchor = working_directory.parts
        if parts[: len(anchor)] != anchor:
            return None
        parts = parts[len(anchor) :]
    if (
        not parts
        or len(parts) > _MAXIMUM_WORKSPACE_PATH_COMPONENTS
        or any(_is_forbidden_component(part) for part in parts)
    ):
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
