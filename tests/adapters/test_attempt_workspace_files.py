"""What `AttemptWorkspaceFileAccess` grants inside one attempt's real lease,
and what it refuses before ever reaching outside it.

Every scenario runs against a real temporary directory tree and the real
`openat2` syscall: the fence is the kernel's own path resolution, so a
filesystem fake could only assert that the adapter believes its own
abstraction, never that a symlink, a mount boundary, a hard link, a swapped
directory, or an escaping path is truly refused. One scenario this host
cannot construct without privileges -- a real mount -- is noted where it
appears rather than faked as a real kernel behaviour.
"""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from atelier2.adapters import attempt_workspace_files
from atelier2.adapters.attempt_workspace_files import (
    AttemptWorkspaceFileAccess,
    AttemptWorkspaceFileOutcome,
    AttemptWorkspaceFileRefusal,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.provider_conversations import (
    ProviderFilesystemAccess,
    ProviderFilesystemAnswer,
    ProviderFilesystemEffect,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderFilesystemRequestId,
)

ATTEMPT = AgentAttemptId("a" * 64)
REQUEST_ID = ProviderFilesystemRequestId(1)
A_READ_CEILING = 1_024


def _lease(working_directory: Path) -> AgentAttemptWorkspaceLease:
    status = os.stat(working_directory, follow_symlinks=False)
    return AgentAttemptWorkspaceLease(
        ATTEMPT, working_directory, status.st_dev, status.st_ino
    )


def _access(
    working_directory: Path, maximum_read_bytes: int = A_READ_CEILING
) -> AttemptWorkspaceFileAccess:
    return AttemptWorkspaceFileAccess(_lease(working_directory), maximum_read_bytes)


def _read(path: Path) -> ProviderFilesystemRequest:
    return ProviderFilesystemRequest(ProviderFilesystemEffect.READ, path, REQUEST_ID)


def _answered(content: bytes) -> AttemptWorkspaceFileOutcome:
    return AttemptWorkspaceFileOutcome(
        ProviderFilesystemReply(REQUEST_ID, ProviderFilesystemAnswer.ANSWERED, content)
    )


@pytest.mark.parametrize(
    "requested",
    [Path("notes.md"), None, Path("./notes.md")],
    ids=["relative", "lease-absolute", "leading-dot-collapses"],
)
def test_a_file_inside_the_lease_is_read(
    tmp_path: Path, requested: Path | None
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"hello workspace")

    outcome = _access(workspace).describe(
        _read(requested if requested is not None else workspace / "notes.md")
    )

    assert outcome == _answered(b"hello workspace")


@pytest.mark.parametrize(
    "requested",
    [Path("sub/deep.txt"), Path("sub/./deep.txt"), Path("sub//deep.txt")],
    ids=["plain", "dot-component-collapses", "double-slash-collapses"],
)
def test_a_nested_relative_path_inside_the_lease_is_read(
    tmp_path: Path, requested: Path
) -> None:
    """`pathlib.PurePath` collapses a `.` component and a doubled separator
    lexically, at parse time, regardless of how the `Path` was built -- there
    is no `Path` value this port's `ProviderFilesystemRequest.path` could ever
    carry from which either survives to reach this adapter. All three forms
    therefore name the same file."""

    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "deep.txt").write_bytes(b"deep bytes")

    outcome = _access(workspace).describe(_read(requested))

    assert outcome == _answered(b"deep bytes")


def test_answer_returns_only_the_port_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"hello")
    access: ProviderFilesystemAccess = _access(workspace)

    reply = access.answer(_read(Path("notes.md")))

    assert reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.ANSWERED, b"hello"
    )


@dataclass(frozen=True)
class _RefusalScenario:
    name: str
    build: Callable[[Path, Path], Path]
    refusal: AttemptWorkspaceFileRefusal


def _parent_directory_escape(tmp_path: Path, _workspace: Path) -> Path:
    (tmp_path / "sibling.txt").write_bytes(b"sibling secret")
    return Path("../sibling.txt")


def _foreign_absolute_address(_tmp_path: Path, _workspace: Path) -> Path:
    return Path("/etc/hostname")


def _a_workspace_name_prefixed_by_another(tmp_path: Path, workspace: Path) -> Path:
    (tmp_path / f"{workspace.name}-decoy").mkdir()
    (tmp_path / f"{workspace.name}-decoy" / "notes.md").write_bytes(b"decoy")
    return tmp_path / f"{workspace.name}-decoy" / "notes.md"


def _an_embedded_nul_byte(_tmp_path: Path, _workspace: Path) -> Path:
    return Path("evil\x00name")


def _a_missing_file(_tmp_path: Path, _workspace: Path) -> Path:
    return Path("never-created.txt")


def _a_symlink_path_component(_tmp_path: Path, workspace: Path) -> Path:
    (workspace / "sub").mkdir()
    (workspace / "sub" / "file.txt").write_bytes(b"real content")
    (workspace / "link").symlink_to(workspace / "sub")
    return Path("link/file.txt")


def _a_symlink_as_the_requested_file(_tmp_path: Path, workspace: Path) -> Path:
    real = workspace / "real.txt"
    real.write_bytes(b"actual content")
    (workspace / "alias.txt").symlink_to(real)
    return Path("alias.txt")


def _a_fifo(_tmp_path: Path, workspace: Path) -> Path:
    os.mkfifo(workspace / "pipe")
    return Path("pipe")


def _a_hard_link_reaching_outside_the_lease(tmp_path: Path, workspace: Path) -> Path:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside content")
    os.link(outside, workspace / "linked.txt")
    return Path("linked.txt")


def _an_unreadable_intermediate_directory(_tmp_path: Path, workspace: Path) -> Path:
    (workspace / "locked").mkdir(mode=0o000)
    return Path("locked/never.txt")


_REFUSAL_SCENARIOS = (
    _RefusalScenario(
        "parent directory escape",
        _parent_directory_escape,
        AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "foreign absolute address",
        _foreign_absolute_address,
        AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "absolute address only sharing the lease name as a prefix",
        _a_workspace_name_prefixed_by_another,
        AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "embedded NUL byte",
        _an_embedded_nul_byte,
        AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "missing file",
        _a_missing_file,
        AttemptWorkspaceFileRefusal.FILE_NOT_FOUND,
    ),
    _RefusalScenario(
        "an unreadable intermediate directory",
        _an_unreadable_intermediate_directory,
        AttemptWorkspaceFileRefusal.ACCESS_DENIED,
    ),
    _RefusalScenario(
        "symlink path component",
        _a_symlink_path_component,
        AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK,
    ),
    _RefusalScenario(
        "symlink as the requested file",
        _a_symlink_as_the_requested_file,
        AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK,
    ),
    _RefusalScenario(
        "a FIFO named where a file was expected",
        _a_fifo,
        AttemptWorkspaceFileRefusal.NOT_A_REGULAR_FILE,
    ),
    _RefusalScenario(
        "a hard link reaching a file outside the lease",
        _a_hard_link_reaching_outside_the_lease,
        AttemptWorkspaceFileRefusal.FILE_IS_HARD_LINKED,
    ),
)


@pytest.mark.parametrize(
    "scenario",
    _REFUSAL_SCENARIOS,
    ids=[scenario.name for scenario in _REFUSAL_SCENARIOS],
)
def test_a_request_unreachable_inside_the_lease_is_refused(
    tmp_path: Path, scenario: _RefusalScenario
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requested = scenario.build(tmp_path, workspace)

    try:
        outcome = _access(workspace).describe(_read(requested))

        assert outcome.reply == ProviderFilesystemReply(
            REQUEST_ID, ProviderFilesystemAnswer.REFUSED
        )
        assert outcome.refusal is scenario.refusal
    finally:
        if scenario.name == "an unreadable intermediate directory":
            (workspace / "locked").chmod(0o755)


def test_a_fifo_probe_never_performs_a_data_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIFO with no writer would block a data-open forever; the `O_PATH`
    probe never performs one, so the refusal is prompt and `os.open` (the
    only call this adapter ever uses for a data descriptor) is never
    reached at all."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "pipe")
    opened_names = _spying_open(monkeypatch)

    outcome = _access(workspace).describe(_read(Path("pipe")))

    assert outcome.refusal is AttemptWorkspaceFileRefusal.NOT_A_REGULAR_FILE
    assert opened_names == []


def test_a_lease_whose_directory_changed_identity_is_refused(tmp_path: Path) -> None:
    """The impostor is a directory that already existed elsewhere and is moved
    onto the leased path, so it carries a real, distinct inode rather than one
    a naive recreate-in-place could coincidentally reuse."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"original content")
    lease = _lease(workspace)

    impostor = tmp_path / "impostor"
    impostor.mkdir()
    (impostor / "notes.md").write_bytes(b"impostor content")
    shutil.rmtree(workspace)
    impostor.rename(workspace)

    outcome = AttemptWorkspaceFileAccess(lease, A_READ_CEILING).describe(
        _read(Path("notes.md"))
    )

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.LEASED_DIRECTORY_CHANGED


def test_a_component_swapped_immediately_before_the_kernel_resolves_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single `openat2` call resolves the whole path atomically inside the
    kernel, so there is no longer a userspace-visible window between steps
    for a test to land a swap "during" the resolution -- that race is exactly
    what moving the walk into one kernel call closes. The strongest race this
    suite can still construct is placing the swap as late as userspace can
    arrange: immediately before control passes to the real syscall. Even
    there, the kernel's own `RESOLVE_NO_SYMLINKS` still catches it."""

    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "file.txt").write_bytes(b"real content")
    lease = _lease(workspace)

    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "file.txt").write_bytes(b"sentinel secret")

    real_openat2 = attempt_workspace_files._openat2_path_descriptor

    def swap_then_resolve(dir_fd: int, relative_path: str) -> int:
        (workspace / "sub" / "file.txt").unlink()
        (workspace / "sub" / "file.txt").symlink_to(sentinel / "file.txt")
        return real_openat2(dir_fd, relative_path)

    monkeypatch.setattr(
        attempt_workspace_files, "_openat2_path_descriptor", swap_then_resolve
    )

    outcome = AttemptWorkspaceFileAccess(lease, A_READ_CEILING).describe(
        _read(Path("sub/file.txt"))
    )

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK
    assert (sentinel / "file.txt").read_bytes() == b"sentinel secret"


def test_a_mount_crossing_reported_by_the_kernel_is_mapped_to_its_own_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`openat2`'s `RESOLVE_NO_XDEV` is documented to refuse crossing any
    mount point during resolution, including a same-filesystem bind mount
    (`man 2 openat2`: "detection of mount point crossings, including bind
    mounts... during path resolution"), which a `st_dev` check on our own
    descriptors could never see. Building a real mount needs privileges this
    suite does not have -- a named gap, carried in the PR body -- so only the
    errno-to-refusal mapping is under test here: the kernel is trusted for
    the enforcement itself, cited above rather than exercised."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def raising_openat2(dir_fd: int, relative_path: str) -> int:
        raise OSError(errno.EXDEV, "simulated mount crossing")

    monkeypatch.setattr(
        attempt_workspace_files, "_openat2_path_descriptor", raising_openat2
    )

    outcome = _access(workspace).describe(_read(Path("sub/file.txt")))

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.PATH_CROSSED_A_MOUNT


def test_a_kernel_without_openat2_support_is_refused_as_workspace_io_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def raising_openat2(dir_fd: int, relative_path: str) -> int:
        raise OSError(errno.ENOSYS, "simulated missing openat2 support")

    monkeypatch.setattr(
        attempt_workspace_files, "_openat2_path_descriptor", raising_openat2
    )

    outcome = _access(workspace).describe(_read(Path("notes.md")))

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.WORKSPACE_IO_FAILED
    assert outcome.detail == "ENOSYS"


def test_a_resolved_file_that_changed_identity_before_the_data_reopen_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Targets the exact `/proc/self/fd/<n>` reopen descriptor by number,
    rather than by call order: an unrelated `fstat` call elsewhere in the
    process (Python's own buffered I/O calls it too) must not be able to
    perturb which call this test spoofs."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"hello")
    real_open = os.open
    real_fstat = os.fstat
    data_descriptors: list[int] = []

    def marking_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        opened = real_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith("/proc/self/fd/"):
            data_descriptors.append(opened)
        return opened

    def spoofing_fstat(descriptor: int) -> os.stat_result:
        if descriptor in data_descriptors:
            elsewhere = real_open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                return real_fstat(elsewhere)
            finally:
                os.close(elsewhere)
        return real_fstat(descriptor)

    monkeypatch.setattr(attempt_workspace_files.os, "open", marking_open)
    monkeypatch.setattr(attempt_workspace_files.os, "fstat", spoofing_fstat)

    outcome = _access(workspace).describe(_read(Path("notes.md")))

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.RESOLVED_FILE_CHANGED_IDENTITY


def test_an_already_oversize_file_is_refused_without_opening_or_reading_its_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's own `fstat` already knows the size is past the ceiling, so
    this is refused there -- before the data descriptor is even opened
    through `/proc/self/fd`, let alone read. The bounded read loop exists
    only to catch a file that grows past the ceiling after this check, not
    to discover one that already is."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "big.bin").write_bytes(b"x" * 10)
    opened_names = _spying_open(monkeypatch)
    read_calls: list[int] = []
    real_read = os.read

    def spying_read(descriptor: int, size: int) -> bytes:
        read_calls.append(descriptor)
        return real_read(descriptor, size)

    monkeypatch.setattr(attempt_workspace_files.os, "read", spying_read)

    outcome = _access(workspace, maximum_read_bytes=5).describe(_read(Path("big.bin")))

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.FILE_EXCEEDS_THE_CEILING
    assert opened_names == []
    assert read_calls == []


def test_a_file_that_grows_during_the_read_is_refused_once_past_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A size read before the read call is never trusted: the file starts
    within the ceiling and grows past it between chunks, so only a bounded
    read loop -- never a single fixed-length read sized from a stale
    `fstat` -- can catch this instead of silently answering a truncated
    prefix as though it were the whole file."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "growing.bin"
    target.write_bytes(b"a" * 3)
    real_read = os.read

    def growing_read(descriptor: int, size: int) -> bytes:
        chunk = real_read(descriptor, min(size, 2))
        if chunk:
            with open(target, "r+b") as handle:
                handle.seek(0, os.SEEK_END)
                handle.write(b"b" * 10)
        return chunk

    monkeypatch.setattr(attempt_workspace_files.os, "read", growing_read)

    outcome = _access(workspace, maximum_read_bytes=5).describe(
        _read(Path("growing.bin"))
    )

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.FILE_EXCEEDS_THE_CEILING


def test_a_write_request_is_refused_as_not_yet_granted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write = ProviderFilesystemRequest(
        ProviderFilesystemEffect.WRITE, Path("notes.md"), REQUEST_ID, b"new content"
    )

    outcome = _access(workspace).describe(write)

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.WRITE_NOT_GRANTED
    assert list(workspace.iterdir()) == []


def _spying_open(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Record every real name `os.open` is asked to open, still opening it.

    `openat2` runs through the raw `ctypes` syscall boundary, never through
    `os.open`, so any name recorded here is a data descriptor this adapter
    actually opened after a successful, already-fenced resolution."""

    opened_names: list[str] = []
    real_open = os.open

    def spy(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str):
            opened_names.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(attempt_workspace_files.os, "open", spy)
    return opened_names


def test_a_pure_escape_calls_openat2_never_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "secret.txt").write_bytes(b"do not read me")
    calls: list[tuple[int, str]] = []

    def never_called(dir_fd: int, relative_path: str) -> int:
        calls.append((dir_fd, relative_path))
        raise AssertionError("openat2 should never be called for a pure escape")

    monkeypatch.setattr(
        attempt_workspace_files, "_openat2_path_descriptor", never_called
    )

    outcome = _access(workspace).describe(_read(Path("../sentinel/secret.txt")))

    assert outcome.reply.answer is ProviderFilesystemAnswer.REFUSED
    assert calls == []


def test_a_symlink_escape_calls_openat2_exactly_once_against_the_lease_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment now rests on the kernel's own fenced resolution rather
    than on bookkeeping in this adapter, so what this adapter's own code must
    get right is narrower: call `openat2` exactly once, anchored at the held
    lease descriptor, naming a relative path that never mentions the
    sentinel and is never absolute. The kernel is what actually refuses the
    symlink, proven separately by the real, unmocked scenario in the
    refusal table."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "secret.txt").write_bytes(b"do not read me")
    (workspace / "escape").symlink_to(sentinel)
    lease_root_identity = (os.stat(workspace).st_dev, os.stat(workspace).st_ino)
    real_openat2 = attempt_workspace_files._openat2_path_descriptor
    calls: list[tuple[tuple[int, int], str]] = []

    def recording_openat2(dir_fd: int, relative_path: str) -> int:
        # The identity is read here, while `dir_fd` is still open: by the
        # time `describe` returns, `entered_leased_directory` has already
        # closed it.
        status = os.fstat(dir_fd)
        calls.append(((status.st_dev, status.st_ino), relative_path))
        return real_openat2(dir_fd, relative_path)

    monkeypatch.setattr(
        attempt_workspace_files, "_openat2_path_descriptor", recording_openat2
    )

    outcome = _access(workspace).describe(_read(Path("escape/secret.txt")))

    assert outcome.refusal is AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK
    assert len(calls) == 1
    called_dir_identity, called_path = calls[0]
    assert called_dir_identity == lease_root_identity
    assert not os.path.isabs(called_path)
    assert "sentinel" not in called_path


def test_the_constructor_rejects_a_ceiling_above_the_artifact_bound(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        AttemptWorkspaceFileAccess(_lease(tmp_path), MAXIMUM_ARTIFACT_BYTES + 1)


def test_the_constructor_rejects_a_non_positive_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        AttemptWorkspaceFileAccess(_lease(tmp_path), 0)


def test_an_answered_outcome_cannot_also_carry_a_refusal() -> None:
    with pytest.raises(ValueError, match="no refusal"):
        AttemptWorkspaceFileOutcome(
            ProviderFilesystemReply(
                REQUEST_ID, ProviderFilesystemAnswer.ANSWERED, b"x"
            ),
            AttemptWorkspaceFileRefusal.WRITE_NOT_GRANTED,
        )


def test_a_refused_outcome_must_name_its_reason() -> None:
    with pytest.raises(ValueError, match="names why"):
        AttemptWorkspaceFileOutcome(
            ProviderFilesystemReply(REQUEST_ID, ProviderFilesystemAnswer.REFUSED)
        )


def test_only_a_workspace_io_failure_may_name_an_errno() -> None:
    with pytest.raises(ValueError, match="only a workspace I/O failure"):
        AttemptWorkspaceFileOutcome(
            ProviderFilesystemReply(REQUEST_ID, ProviderFilesystemAnswer.REFUSED),
            AttemptWorkspaceFileRefusal.WRITE_NOT_GRANTED,
            "EIO",
        )


def test_openat2_path_descriptor_reads_a_real_file_through_the_raw_syscall(
    tmp_path: Path,
) -> None:
    """A direct proof of the `ctypes` boundary itself, beneath the adapter:
    the syscall returns a usable, readable descriptor for an ordinary file."""

    (tmp_path / "notes.md").write_bytes(b"hello workspace")
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        path_fd = attempt_workspace_files._openat2_path_descriptor(root_fd, "notes.md")
    finally:
        os.close(root_fd)
    try:
        data_fd = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY)
        try:
            assert os.read(data_fd, 64) == b"hello workspace"
        finally:
            os.close(data_fd)
    finally:
        os.close(path_fd)


def test_openat2_path_descriptor_raises_os_error_with_errno_set(
    tmp_path: Path,
) -> None:
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as raised:
            attempt_workspace_files._openat2_path_descriptor(root_fd, "does-not-exist")
        assert raised.value.errno == errno.ENOENT
    finally:
        os.close(root_fd)


def test_the_openat2_syscall_number_is_known_only_for_checked_architectures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        attempt_workspace_files.platform, "machine", lambda: "unchecked-architecture"
    )

    with pytest.raises(RuntimeError, match="openat2 syscall number"):
        attempt_workspace_files._openat2_syscall_number()
