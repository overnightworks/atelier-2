from __future__ import annotations

import os
import stat
from collections import deque
from pathlib import Path

from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttemptId,
)
from atelier2.ports.agent_attempts import AgentAttemptReader
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease

SCRATCH_ROOT_MODE = 0o700
"""The only mode a scratch root may carry: no group, no world, no sharing."""

_GIT_ENTRY = ".git"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LEASE_MARKER_SUFFIX = ".lease"
# The mark holds the device and inode of the directory this owner created, so a
# later removal can ask whether the directory standing there is still that one.
_LEASE_MARK_VERSION = "agent-attempt-workspace/v1"
_LEASE_MARKER_BYTES = 128
_LEASE_MARKER_MODE = 0o600
_MAXIMUM_HELD_WORKSPACE_LEVELS = 64
"""How many nested directories one removal pass keeps open at the same time."""


class AgentScratchRootRefused(ValueError):
    """The declared scratch root cannot hold attempt workspaces."""


class AgentAttemptWorkspaceRefused(RuntimeError):
    """This attempt cannot be given the blank workspace it requires."""


def _marks_a_git_worktree(git_entry: Path) -> bool:
    """Whether this `.git` entry is one git itself would treat as a worktree.

    A file or a link is a `gitdir:` pointer. A directory is a worktree only
    when it holds `HEAD` -- the same fact git uses to decide it has found a
    repository. An empty leftover `.git` directory is not a worktree: git
    walks past it, and treating it as one would refuse every scratch root
    under that parent, including every pytest root under `/tmp` once a stray
    `/tmp/.git` directory exists.
    """

    if git_entry.is_symlink() or git_entry.is_file():
        return True
    return (git_entry / "HEAD").exists() or (git_entry / "HEAD").is_symlink()


def _attested_directory(scratch_root: Path) -> Path:
    """Read the named path before it is opened, and answer what it truly names.

    The path is followed to the directory it resolves to, and refused unless
    that directory stays beneath the declared root -- which a root can only do
    by resolving to itself: a symbolic link anywhere above it would let whoever
    controls the link move every attempt's directory somewhere else between two
    calls. A root inside a git worktree would put provider scratch files into a
    checkout the operator is working in. Both are refused by name, because
    whoever first points `--agent-scratch-root` at such a path deserves to read
    why. The walk only reads: a `.git` marker that was not there before this
    call is not created by it.
    """

    resolved = scratch_root.resolve()
    if not resolved.is_relative_to(scratch_root):
        raise AgentScratchRootRefused(
            f"the agent scratch root must be reached through no symbolic link, "
            f"but {scratch_root} resolves to {resolved}: a link above the root "
            "can move every attempt's directory between two calls"
        )
    for component in (scratch_root, *scratch_root.parents):
        git_entry = component / _GIT_ENTRY
        if _marks_a_git_worktree(git_entry):
            raise AgentScratchRootRefused(
                f"the agent scratch root must lie outside every git worktree, but "
                f"{git_entry} exists: a provider writes into its workspace, so a "
                "root inside a checkout would write into that checkout"
            )
    return resolved


def _open_scratch_root(scratch_root: Path) -> int:
    attested = _attested_directory(scratch_root)
    try:
        return os.open(attested, _DIRECTORY_FLAGS)
    except OSError as error:
        raise AgentScratchRootRefused(
            f"the agent scratch root must be an existing directory the serving "
            f"user owns with mode {SCRATCH_ROOT_MODE:o}, but {scratch_root} "
            f"could not be opened: {error}"
        ) from error


def _encode_lease_mark(created: os.stat_result) -> bytes:
    """Name the exact directory a lease stands for."""

    return f"{_LEASE_MARK_VERSION} {created.st_dev} {created.st_ino}\n".encode("ascii")


def _decode_lease_mark(payload: bytes) -> tuple[int, int]:
    """Read back one directory identity, or refuse a mark this owner never wrote."""

    if len(payload) > _LEASE_MARKER_BYTES:
        raise ValueError("agent attempt lease mark exceeds its exact bound")
    fields = payload.decode("ascii", "strict").split()
    if len(fields) != 3 or fields[0] != _LEASE_MARK_VERSION:
        raise ValueError("agent attempt lease mark is not this owner's")
    device, inode = fields[1], fields[2]
    if not device.isdigit() or not inode.isdigit():
        raise ValueError("agent attempt lease mark names no directory identity")
    return int(device), int(inode)


def _lease_marker_name(attempt_id: AgentAttemptId) -> str:
    return f"{attempt_id.value}{_LEASE_MARKER_SUFFIX}"


def _is_attempt_entry(entry: os.DirEntry[str]) -> bool:
    """A scratch root holds attempt workspaces and their lease markers, nothing else."""

    try:
        AgentAttemptId(entry.name.removesuffix(_LEASE_MARKER_SUFFIX))
    except ValueError:
        return False
    if entry.name.endswith(_LEASE_MARKER_SUFFIX):
        return entry.is_file(follow_symlinks=False)
    return entry.is_dir(follow_symlinks=False)


def _remove_workspace_tree(parent_fd: int, name: str) -> None:
    """Remove one directory and its contents through descriptors alone.

    Every step is relative to a descriptor the caller already holds and no
    symbolic link is ever followed, so nothing outside this directory can be
    reached by replacing an entry inside it. A link the provider left behind is
    unlinked; whatever it points at is not touched.

    A provider may nest as deeply as the filesystem allows, so neither the
    interpreter's recursion limit nor this process's descriptor limit may decide
    whether a workspace can still be cleaned: one pass holds a bounded window of
    descriptors, and the passes repeat until the directory is gone.
    """

    while not _remove_reachable_levels(parent_fd, name):
        pass


def _remove_reachable_levels(parent_fd: int, name: str) -> bool:
    """Remove what one window of descriptors reaches; True once nothing is left.

    The window slides: once it is full the shallowest descriptor is dropped to
    keep descending, and the levels above the drop are removed by a later pass.
    """

    try:
        opened = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return True
    held: deque[tuple[str, int]] = deque([(name, opened)])
    complete = True
    try:
        while held:
            current_name, current_fd = held[-1]
            subdirectory = _unlink_files_and_name_one_subdirectory(current_fd)
            if subdirectory is not None:
                try:
                    descended = os.open(
                        subdirectory, _DIRECTORY_FLAGS, dir_fd=current_fd
                    )
                except FileNotFoundError:
                    continue
                if len(held) == _MAXIMUM_HELD_WORKSPACE_LEVELS:
                    complete = False
                    os.close(held.popleft()[1])
                held.append((subdirectory, descended))
                continue
            os.close(held.pop()[1])
            if held:
                os.rmdir(current_name, dir_fd=held[-1][1])
            elif complete:
                os.rmdir(current_name, dir_fd=parent_fd)
        return complete
    finally:
        for _name, descriptor in held:
            os.close(descriptor)


def _unlink_files_and_name_one_subdirectory(directory_fd: int) -> str | None:
    subdirectory: str | None = None
    for entry in os.scandir(directory_fd):
        if entry.is_dir(follow_symlinks=False):
            subdirectory = subdirectory or entry.name
        else:
            os.unlink(entry.name, dir_fd=directory_fd)
    return subdirectory


class LocalAgentAttemptWorkspaceOwner:
    """The one owner of every attempt's scratch directory on this host.

    It binds the declared scratch root to a held directory descriptor, so every
    creation and removal happens inside the exact directory that was attested,
    not inside whatever the same path names later. The root itself is never
    removed and never removed recursively: only one attempt's own directory is.

    It proves no operating-system isolation. A provider still runs as the
    serving user and can name any path it likes; what this owner guarantees is
    that the directory it is *started in* is blank, its own, and gone again.
    """

    def __init__(self, scratch_root: Path) -> None:
        # Absolute but deliberately unresolved: resolving would erase the very
        # symbolic links the attestation below refuses, and this exact path is
        # what is attested and, once it resolves to itself, what is opened.
        self._scratch_root = Path(os.path.abspath(scratch_root))
        self._root_fd = _open_scratch_root(self._scratch_root)
        try:
            self.preflight()
        except BaseException:
            self.close()
            raise

    @property
    def scratch_root(self) -> Path:
        return self._scratch_root

    def preflight(self) -> None:
        status = os.fstat(self._root_fd)
        if status.st_uid != os.getuid():
            raise AgentScratchRootRefused(
                f"the agent scratch root {self._scratch_root} belongs to another "
                "user, so this server cannot own the workspaces inside it"
            )
        if stat.S_IMODE(status.st_mode) != SCRATCH_ROOT_MODE:
            raise AgentScratchRootRefused(
                f"the agent scratch root {self._scratch_root} must carry mode "
                f"{SCRATCH_ROOT_MODE:o}, not {stat.S_IMODE(status.st_mode):o}: "
                "group or world access would share every provider's scratch files"
            )
        for entry in os.scandir(self._root_fd):
            if not _is_attempt_entry(entry):
                raise AgentScratchRootRefused(
                    f"the agent scratch root {self._scratch_root} holds {entry.name}, "
                    "which is no attempt workspace: this root is owned entirely by "
                    "attempt directories, so nothing else may be mistaken for one"
                )

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        self.preflight()
        name = attempt_id.value
        try:
            os.mkdir(name, SCRATCH_ROOT_MODE, dir_fd=self._root_fd)
        except FileExistsError as error:
            raise AgentAttemptWorkspaceRefused(
                f"the workspace of attempt {name} already exists under "
                f"{self._scratch_root}: an attempt is started in a directory it "
                "created itself or in none at all"
            ) from error
        working_directory = self._scratch_root / name
        created = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        try:
            named = os.stat(working_directory, follow_symlinks=False)
            same_directory = (named.st_dev, named.st_ino) == (
                created.st_dev,
                created.st_ino,
            )
        except OSError:
            same_directory = False
        if not same_directory:
            _remove_workspace_tree(self._root_fd, name)
            raise AgentScratchRootRefused(
                f"the agent scratch root {self._scratch_root} no longer names the "
                "directory this server bound, so the path handed to a provider "
                "would not be the workspace that was created"
            )
        try:
            self._mark_leased(attempt_id, created)
        except FileExistsError as error:
            # A mark standing without its directory is reachable without an
            # impostor: release removes the tree and unlinks the mark, so a
            # crash between the two leaves one behind. Adopting it would hand
            # the next lease a provenance it never created, so the directory
            # just created is taken back and the state is named.
            _remove_workspace_tree(self._root_fd, name)
            raise AgentAttemptWorkspaceRefused(
                f"a lease mark for attempt {name} already stands under "
                f"{self._scratch_root} without the directory it names: this "
                "owner writes the mark that licenses a removal, and it never "
                "adopts one it did not write"
            ) from error
        return AgentAttemptWorkspaceLease(
            attempt_id, working_directory, created.st_dev, created.st_ino
        )

    def release(self, attempt_id: AgentAttemptId) -> None:
        """Remove the directory this owner leased to that attempt, and only that.

        The mark this owner wrote while creating the directory is what says the
        directory is its to remove, and the mark names one *directory* rather
        than one path: it carries the device and inode that were created. A path
        that merely bears an attempt's name carries no mark, and a directory
        moved into that path after the fact does not match the one the mark
        stands for -- so neither the cancellation nor the restart removes
        somebody else's ground.
        """

        marker = _lease_marker_name(attempt_id)
        try:
            with open(
                os.open(
                    marker,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=self._root_fd,
                ),
                "rb",
            ) as stream:
                marked = stream.read(_LEASE_MARKER_BYTES + 1)
        except FileNotFoundError:
            return
        try:
            leased = _decode_lease_mark(marked)
        except ValueError as error:
            raise AgentAttemptWorkspaceRefused(
                f"the lease mark of attempt {attempt_id.value} under "
                f"{self._scratch_root} is not one this owner wrote, so the "
                "directory it names is not this owner's to remove"
            ) from error
        try:
            standing = os.stat(
                attempt_id.value, dir_fd=self._root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            os.unlink(marker, dir_fd=self._root_fd)
            return
        if (standing.st_dev, standing.st_ino) != leased:
            raise AgentAttemptWorkspaceRefused(
                f"the directory at {self._scratch_root / attempt_id.value} is not "
                "the one this owner leased to that attempt: its identity changed "
                "under the mark, so removing it would remove somebody else's"
            )
        _remove_workspace_tree(self._root_fd, attempt_id.value)
        os.unlink(marker, dir_fd=self._root_fd)

    def _mark_leased(self, attempt_id: AgentAttemptId, created: os.stat_result) -> None:
        """Write the provenance of exactly the directory that was just created.

        The mark is created exclusively: a file somebody else placed under this
        name is never adopted as provenance, because the whole weight of the
        later removal rests on who wrote it.
        """

        descriptor = os.open(
            _lease_marker_name(attempt_id),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            _LEASE_MARKER_MODE,
            dir_fd=self._root_fd,
        )
        with open(descriptor, "wb") as stream:
            stream.write(_encode_lease_mark(created))

    def reconcile(self, attempts: AgentAttemptReader) -> None:
        """Remove what terminal attempts left behind, and nothing else.

        Every directory is read back against the durable attempt it is named
        after. A terminal attempt's workspace is removed, a nonterminal one's
        is left exactly as it stands for the recovery that owns it, and a name
        with no durable attempt behind it refuses the whole reconciliation
        rather than being deleted on the assumption that it is rubbish.
        Removal goes through the same lease mark every caller obeys, so a
        directory this root never leased survives the restart untouched.
        """

        self.preflight()
        for name in sorted(os.listdir(self._root_fd)):
            attempt_id = AgentAttemptId(name.removesuffix(_LEASE_MARKER_SUFFIX))
            if attempts.load(attempt_id).state in TERMINAL_AGENT_ATTEMPT_STATES:
                self.release(attempt_id)

    def close(self) -> None:
        os.close(self._root_fd)
