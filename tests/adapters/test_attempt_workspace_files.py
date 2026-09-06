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
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters import attempt_workspace_files
from atelier2.adapters.attempt_workspace_files import AttemptWorkspaceFileAccess
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import (
    ATTEMPT_WORKSPACE,
    GRANTS_NOTHING,
    PermissionCorrelationId,
    PermissionDecision,
    PermissionEffect,
    PermissionPolicyRevision,
    PermissionRequest,
    PolicyPermissionDecider,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.provider_conversations import (
    ProviderFilesystemAccess,
    ProviderFilesystemAnswer,
    ProviderFilesystemEffect,
    ProviderFilesystemRefusal,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderFilesystemRequestId,
)

ATTEMPT = AgentAttemptId("a" * 64)
REQUEST_ID = ProviderFilesystemRequestId(1)
A_READ_CEILING = 1_024
GRANTS_THE_WORKSPACE = PermissionPolicyRevision(
    frozenset(
        {
            (PermissionEffect.WORKSPACE_READ, ATTEMPT_WORKSPACE),
            (PermissionEffect.WORKSPACE_WRITE, ATTEMPT_WORKSPACE),
        }
    )
)


@dataclass
class _Ledger:
    """The authority a scenario binds: one policy, and what it was asked."""

    policy: PermissionPolicyRevision = GRANTS_THE_WORKSPACE
    decided: list[PermissionRequest] = field(default_factory=list)
    refused: list[PermissionRequest] = field(default_factory=list)
    before_answering: Callable[[], None] = lambda: None

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        self.before_answering()
        self.decided.append(request)
        return PolicyPermissionDecider(self.policy).decide(request)

    def refuse(self, request: PermissionRequest) -> PermissionDecision:
        self.refused.append(request)
        return PolicyPermissionDecider(self.policy).refuse(request)


class _TheLedgerIsGone(RuntimeError):
    """What the ledger raises when a receipt cannot be kept."""


def _raising_ledger() -> _Ledger:
    def unavailable() -> None:
        raise _TheLedgerIsGone("durable state is unavailable")

    return _Ledger(before_answering=unavailable)


def _question(
    effect: PermissionEffect, call_ordinal: int = REQUEST_ID.call_ordinal
) -> PermissionRequest:
    return PermissionRequest(
        effect,
        ATTEMPT_WORKSPACE,
        PermissionCorrelationId.for_file_call(ATTEMPT, call_ordinal),
    )


def _lease(working_directory: Path) -> AgentAttemptWorkspaceLease:
    status = os.stat(working_directory, follow_symlinks=False)
    return AgentAttemptWorkspaceLease(
        ATTEMPT, working_directory, status.st_dev, status.st_ino
    )


def _access(
    working_directory: Path,
    maximum_read_bytes: int = A_READ_CEILING,
    ledger: _Ledger | None = None,
) -> AttemptWorkspaceFileAccess:
    return AttemptWorkspaceFileAccess(
        _lease(working_directory), maximum_read_bytes, ledger or _Ledger()
    )


def _read(path: Path) -> ProviderFilesystemRequest:
    return ProviderFilesystemRequest(ProviderFilesystemEffect.READ, path, REQUEST_ID)


def _write(path: Path, content: bytes) -> ProviderFilesystemRequest:
    return ProviderFilesystemRequest(
        ProviderFilesystemEffect.WRITE, path, REQUEST_ID, content
    )


def _answered(content: bytes) -> ProviderFilesystemReply:
    return ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.ANSWERED, content
    )


def _answered_write() -> ProviderFilesystemReply:
    return ProviderFilesystemReply(REQUEST_ID, ProviderFilesystemAnswer.ANSWERED)


def _refused(
    refusal: ProviderFilesystemRefusal, detail: str = ""
) -> ProviderFilesystemReply:
    return ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED, refusal=refusal, detail=detail
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

    reply = _access(workspace).answer(
        _read(requested if requested is not None else workspace / "notes.md")
    )

    assert reply == _answered(b"hello workspace")


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

    reply = _access(workspace).answer(_read(requested))

    assert reply == _answered(b"deep bytes")


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
    refusal: ProviderFilesystemRefusal


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
        ProviderFilesystemRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "foreign absolute address",
        _foreign_absolute_address,
        ProviderFilesystemRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "absolute address only sharing the lease name as a prefix",
        _a_workspace_name_prefixed_by_another,
        ProviderFilesystemRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "embedded NUL byte",
        _an_embedded_nul_byte,
        ProviderFilesystemRefusal.PATH_LEFT_THE_LEASE,
    ),
    _RefusalScenario(
        "missing file",
        _a_missing_file,
        ProviderFilesystemRefusal.FILE_NOT_FOUND,
    ),
    _RefusalScenario(
        "an unreadable intermediate directory",
        _an_unreadable_intermediate_directory,
        ProviderFilesystemRefusal.ACCESS_DENIED,
    ),
    _RefusalScenario(
        "symlink path component",
        _a_symlink_path_component,
        ProviderFilesystemRefusal.PATH_NAMED_A_SYMLINK,
    ),
    _RefusalScenario(
        "symlink as the requested file",
        _a_symlink_as_the_requested_file,
        ProviderFilesystemRefusal.PATH_NAMED_A_SYMLINK,
    ),
    _RefusalScenario(
        "a FIFO named where a file was expected",
        _a_fifo,
        ProviderFilesystemRefusal.NOT_A_REGULAR_FILE,
    ),
    _RefusalScenario(
        "a hard link reaching a file outside the lease",
        _a_hard_link_reaching_outside_the_lease,
        ProviderFilesystemRefusal.FILE_IS_HARD_LINKED,
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
    """Refused by the fence, and kept as a refusal the policy was never asked:
    the policy grants the workspace, and this path was never the workspace."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requested = scenario.build(tmp_path, workspace)
    ledger = _Ledger()

    try:
        reply = _access(workspace, ledger=ledger).answer(_read(requested))

        assert reply == _refused(scenario.refusal)
        assert ledger.decided == []
        assert ledger.refused == [_question(PermissionEffect.WORKSPACE_READ)]
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
    open_calls = _spying_open(monkeypatch)

    reply = _access(workspace).answer(_read(Path("pipe")))

    assert reply.refusal is ProviderFilesystemRefusal.NOT_A_REGULAR_FILE
    assert open_calls == []


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

    reply = AttemptWorkspaceFileAccess(lease, A_READ_CEILING, _Ledger()).answer(
        _read(Path("notes.md"))
    )

    assert reply == _refused(ProviderFilesystemRefusal.LEASED_DIRECTORY_CHANGED)


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

    reply = AttemptWorkspaceFileAccess(lease, A_READ_CEILING, _Ledger()).answer(
        _read(Path("sub/file.txt"))
    )

    assert reply == _refused(ProviderFilesystemRefusal.PATH_NAMED_A_SYMLINK)
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

    reply = _access(workspace).answer(_read(Path("sub/file.txt")))

    assert reply == _refused(ProviderFilesystemRefusal.PATH_CROSSED_A_MOUNT)


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

    reply = _access(workspace).answer(_read(Path("notes.md")))

    assert reply == _refused(ProviderFilesystemRefusal.WORKSPACE_IO_FAILED, "ENOSYS")


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

    reply = _access(workspace).answer(_read(Path("notes.md")))

    assert reply == _refused(ProviderFilesystemRefusal.RESOLVED_FILE_CHANGED_IDENTITY)


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
    open_calls = _spying_open(monkeypatch)
    read_calls: list[int] = []
    real_read = os.read

    def spying_read(descriptor: int, size: int) -> bytes:
        read_calls.append(descriptor)
        return real_read(descriptor, size)

    monkeypatch.setattr(attempt_workspace_files.os, "read", spying_read)

    reply = _access(workspace, maximum_read_bytes=5).answer(_read(Path("big.bin")))

    assert reply == _refused(ProviderFilesystemRefusal.FILE_EXCEEDS_THE_CEILING)
    assert open_calls == []
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

    reply = _access(workspace, maximum_read_bytes=5).answer(_read(Path("growing.bin")))

    assert reply == _refused(ProviderFilesystemRefusal.FILE_EXCEEDS_THE_CEILING)


@dataclass(frozen=True)
class _WriteTargetScenario:
    name: str
    build_parent: Callable[[Path], None]
    requested: Path


def _no_parent_needed(_workspace: Path) -> None:
    pass


def _an_existing_sub_parent(workspace: Path) -> None:
    (workspace / "sub").mkdir()


_WRITE_TARGET_SCENARIOS = (
    _WriteTargetScenario("at the lease root", _no_parent_needed, Path("notes.md")),
    _WriteTargetScenario(
        "nested under an existing parent", _an_existing_sub_parent, Path("sub/deep.txt")
    ),
)


@pytest.mark.parametrize(
    "scenario",
    _WRITE_TARGET_SCENARIOS,
    ids=[scenario.name for scenario in _WRITE_TARGET_SCENARIOS],
)
def test_a_write_creates_the_exact_bytes(
    tmp_path: Path, scenario: _WriteTargetScenario
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scenario.build_parent(workspace)
    target = workspace / scenario.requested

    reply = _access(workspace).answer(_write(scenario.requested, b"exact bytes"))

    assert reply == _answered_write()
    assert target.read_bytes() == b"exact bytes"
    assert {entry.name for entry in target.parent.iterdir()} == {target.name}


def test_a_write_overwrites_an_existing_file_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"original content")

    reply = _access(workspace).answer(_write(Path("notes.md"), b"replaced content"))

    assert reply == _answered_write()
    assert (workspace / "notes.md").read_bytes() == b"replaced content"
    assert list(workspace.iterdir()) == [workspace / "notes.md"]


@pytest.mark.parametrize("failing_call", ["write", "fsync", "replace"])
def test_a_failure_during_staging_leaves_the_target_unchanged_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"original content")

    def raising(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "simulated staging failure")

    monkeypatch.setattr(attempt_workspace_files.os, failing_call, raising)

    reply = _access(workspace).answer(_write(Path("notes.md"), b"new content"))

    assert reply == _refused(ProviderFilesystemRefusal.WORKSPACE_IO_FAILED, "EIO")
    assert (workspace / "notes.md").read_bytes() == b"original content"
    assert list(workspace.iterdir()) == [workspace / "notes.md"]


def test_a_write_completes_despite_short_underlying_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX `write(2)` may transfer fewer bytes than asked; only a caller
    that loops past that short count can promise every byte lands."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = b"exact bytes, one at a time"
    real_write = os.write

    def one_byte_at_a_time(descriptor: int, data: bytes) -> int:
        return real_write(descriptor, data[:1])

    monkeypatch.setattr(attempt_workspace_files.os, "write", one_byte_at_a_time)

    reply = _access(workspace).answer(_write(Path("notes.md"), content))

    assert reply == _answered_write()
    assert (workspace / "notes.md").read_bytes() == content


def test_a_target_swapped_for_a_symlink_after_the_check_still_leaves_its_referent_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lstat` and `replace` are not one atomic step, so a request could swap
    `final_name` for a symlink in between. `rename(2)` never follows a
    symlink at its destination -- it replaces the directory entry itself --
    so even that race lands the new content under `final_name`'s own name,
    leaving whatever the symlink pointed at completely untouched."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"original content")
    referent = workspace / "referent.txt"
    referent.write_bytes(b"referent content")
    real_replace = os.replace

    def swap_then_replace(
        src: str,
        dst: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        os.unlink(dst, dir_fd=dst_dir_fd)
        os.symlink(os.fspath(referent), dst, dir_fd=dst_dir_fd)
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(attempt_workspace_files.os, "replace", swap_then_replace)

    reply = _access(workspace).answer(_write(Path("notes.md"), b"new content"))

    assert reply == _answered_write()
    assert not (workspace / "notes.md").is_symlink()
    assert (workspace / "notes.md").read_bytes() == b"new content"
    assert referent.read_bytes() == b"referent content"


@pytest.mark.parametrize(
    "effect", [ProviderFilesystemEffect.READ, ProviderFilesystemEffect.WRITE]
)
def test_a_path_no_filesystem_encoding_could_hold_is_refused(
    tmp_path: Path, effect: ProviderFilesystemEffect
) -> None:
    """A lone surrogate survives `pathlib.Path.parts` unchanged, so a request
    could carry one all the way to `openat2`'s own `os.fsencode` call, which
    raises `UnicodeEncodeError` rather than an `OSError` -- refused here,
    before either effect ever opens anything."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requested = Path("bad\ud800name")
    request = (
        _read(requested)
        if effect is ProviderFilesystemEffect.READ
        else _write(requested, b"content")
    )

    reply = _access(workspace).answer(request)

    assert reply == _refused(ProviderFilesystemRefusal.PATH_NOT_ENCODABLE)
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    "requested",
    [
        Path(".ssh/id_rsa"),
        Path(".bashrc"),
        Path(".profile"),
        Path(".zshrc"),
        Path(".bash_profile"),
        Path(".grok/config.json"),
        Path(".claude/settings.json"),
        Path(".cursor/config.json"),
        Path(".git/hooks/pre-commit"),
        Path(".git/config"),
        Path(".git/HEAD"),
        Path(".git"),
        Path("sub/.ssh/id_rsa"),
        Path(".SSH/id_rsa"),
        Path(".Git/config"),
    ],
    ids=[
        "ssh-directory",
        "bashrc",
        "profile",
        "zshrc",
        "bash-profile",
        "grok-directory",
        "claude-directory",
        "cursor-directory",
        "git-hooks",
        "git-config",
        "git-head",
        "git-directory-itself",
        "nested-ssh-directory",
        "case-insensitive-ssh",
        "case-insensitive-git",
    ],
)
def test_a_write_naming_a_protected_path_is_refused(
    tmp_path: Path, requested: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    reply = _access(workspace).answer(_write(requested, b"malicious"))

    assert reply == _refused(ProviderFilesystemRefusal.PROTECTED_PATH)
    assert list(workspace.rglob("*")) == []


def test_a_write_naming_a_git_prefixed_but_distinct_file_is_not_protected(
    tmp_path: Path,
) -> None:
    """The boundary is the whole segment `.git`, never a prefix match: a file
    merely named `.gitignore` shares no segment with the protected `.git`
    directory and is an ordinary write."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    reply = _access(workspace).answer(_write(Path(".gitignore"), b"*.log"))

    assert reply == _answered_write()
    assert (workspace / ".gitignore").read_bytes() == b"*.log"


def test_a_write_onto_a_symlinked_name_is_refused_leaving_the_real_file_unchanged(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real = workspace / "real.txt"
    real.write_bytes(b"actual content")
    (workspace / "alias.txt").symlink_to(real)

    reply = _access(workspace).answer(_write(Path("alias.txt"), b"attempted overwrite"))

    assert reply == _refused(ProviderFilesystemRefusal.TARGET_IS_SYMLINK)
    assert real.read_bytes() == b"actual content"
    assert (workspace / "alias.txt").is_symlink()


def test_an_oversize_write_is_refused_without_creating_anything(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    reply = _access(workspace, maximum_read_bytes=5).answer(
        _write(Path("notes.md"), b"too many bytes")
    )

    assert reply == _refused(ProviderFilesystemRefusal.FILE_EXCEEDS_THE_CEILING)
    assert list(workspace.iterdir()) == []


def test_a_write_whose_parent_directory_is_missing_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    reply = _access(workspace).answer(_write(Path("sub/notes.md"), b"content"))

    assert reply == _refused(ProviderFilesystemRefusal.PARENT_MISSING)
    assert list(workspace.iterdir()) == []


_WRITE_REUSES_READ_ESCAPE_SCENARIOS = tuple(
    scenario
    for scenario in _REFUSAL_SCENARIOS
    if scenario.name
    in {
        "parent directory escape",
        "foreign absolute address",
        "absolute address only sharing the lease name as a prefix",
        "embedded NUL byte",
        "symlink path component",
    }
)


@pytest.mark.parametrize(
    "scenario",
    _WRITE_REUSES_READ_ESCAPE_SCENARIOS,
    ids=[scenario.name for scenario in _WRITE_REUSES_READ_ESCAPE_SCENARIOS],
)
def test_a_write_reaching_outside_the_lease_is_refused_the_same_way_as_a_read(
    tmp_path: Path, scenario: _RefusalScenario
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requested = scenario.build(tmp_path, workspace)

    reply = _access(workspace).answer(_write(requested, b"malicious"))

    assert reply == _refused(scenario.refusal)


def test_a_write_parent_mount_crossing_is_mapped_to_its_own_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors the read-side mount test: building a real mount needs
    privileges this suite does not have, so only the errno-to-refusal mapping
    for the parent resolution is under test here."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def raising_openat2_directory(dir_fd: int, relative_path: str) -> int:
        raise OSError(errno.EXDEV, "simulated mount crossing")

    monkeypatch.setattr(
        attempt_workspace_files,
        "_openat2_directory_descriptor",
        raising_openat2_directory,
    )

    reply = _access(workspace).answer(_write(Path("sub/file.txt"), b"content"))

    assert reply == _refused(ProviderFilesystemRefusal.PATH_CROSSED_A_MOUNT)


@dataclass(frozen=True)
class _OpenCall:
    """One real `os.open` call this adapter made, and where it landed.

    `dir_fd_identity` is read while the descriptor is still open, inside the
    spy itself: by the time a test can inspect the call afterward, the
    adapter has already closed every descriptor it held."""

    name: str
    flags: int
    dir_fd_identity: tuple[int, int] | None


def _spying_open(monkeypatch: pytest.MonkeyPatch) -> list[_OpenCall]:
    """Record every real `os.open` call this adapter makes, still opening it.

    `openat2` runs through the raw `ctypes` syscall boundary, never through
    `os.open`, so any call recorded here is a data descriptor this adapter
    actually opened after a successful, already-fenced resolution."""

    calls: list[_OpenCall] = []
    real_open = os.open

    def spy(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str):
            dir_fd_identity = None
            if dir_fd is not None:
                status = os.fstat(dir_fd)
                dir_fd_identity = (status.st_dev, status.st_ino)
            calls.append(_OpenCall(path, flags, dir_fd_identity))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(attempt_workspace_files.os, "open", spy)
    return calls


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

    reply = _access(workspace).answer(_read(Path("../sentinel/secret.txt")))

    assert reply.answer is ProviderFilesystemAnswer.REFUSED
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
        # time `answer` returns, `entered_leased_directory` has already
        # closed it.
        status = os.fstat(dir_fd)
        calls.append(((status.st_dev, status.st_ino), relative_path))
        return real_openat2(dir_fd, relative_path)

    monkeypatch.setattr(
        attempt_workspace_files, "_openat2_path_descriptor", recording_openat2
    )

    reply = _access(workspace).answer(_read(Path("escape/secret.txt")))

    assert reply.refusal is ProviderFilesystemRefusal.PATH_NAMED_A_SYMLINK
    assert len(calls) == 1
    called_dir_identity, called_path = calls[0]
    assert called_dir_identity == lease_root_identity
    assert not os.path.isabs(called_path)
    assert "sentinel" not in called_path


def test_a_write_opens_only_relative_names_through_a_lease_internal_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `os.open` a staged write performs must be a bare relative staged
    name resolved through `dir_fd`, never an absolute path that could address
    something outside the lease; every one of those `dir_fd` values must
    itself be a directory the lease actually owns; the staging create must
    carry `O_EXCL | O_NOFOLLOW | O_CLOEXEC`; and the one `openat2` directory
    resolution for the parent must be anchored at the lease root, naming a
    relative path that never mentions the host's real directory."""

    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    open_calls = _spying_open(monkeypatch)
    lease_internal_identities = {
        (os.stat(workspace).st_dev, os.stat(workspace).st_ino),
        (os.stat(workspace / "sub").st_dev, os.stat(workspace / "sub").st_ino),
    }
    real_openat2_directory = attempt_workspace_files._openat2_directory_descriptor
    directory_calls: list[tuple[tuple[int, int], str]] = []

    def recording_openat2_directory(dir_fd: int, relative_path: str) -> int:
        status = os.fstat(dir_fd)
        directory_calls.append(((status.st_dev, status.st_ino), relative_path))
        return real_openat2_directory(dir_fd, relative_path)

    monkeypatch.setattr(
        attempt_workspace_files,
        "_openat2_directory_descriptor",
        recording_openat2_directory,
    )

    reply = _access(workspace).answer(_write(Path("sub/deep.txt"), b"deep bytes"))

    assert reply == _answered_write()
    assert len(directory_calls) == 1
    called_dir_identity, called_path = directory_calls[0]
    assert called_dir_identity in lease_internal_identities
    assert called_path == "sub"
    assert open_calls
    assert all(not os.path.isabs(call.name) for call in open_calls)
    assert all(str(tmp_path) not in call.name for call in open_calls)
    assert all(call.dir_fd_identity in lease_internal_identities for call in open_calls)
    staging_calls = [call for call in open_calls if call.name.startswith("deep.txt.")]
    assert len(staging_calls) == 1
    required_flags = os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    assert staging_calls[0].flags & required_flags == required_flags


def test_the_constructor_rejects_a_ceiling_above_the_artifact_bound(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        AttemptWorkspaceFileAccess(
            _lease(tmp_path), MAXIMUM_ARTIFACT_BYTES + 1, _Ledger()
        )


def test_the_constructor_rejects_a_non_positive_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        AttemptWorkspaceFileAccess(_lease(tmp_path), 0, _Ledger())


def test_an_answered_reply_cannot_also_carry_a_refusal() -> None:
    with pytest.raises(ValueError, match="no refusal"):
        ProviderFilesystemReply(
            REQUEST_ID,
            ProviderFilesystemAnswer.ANSWERED,
            b"x",
            refusal=ProviderFilesystemRefusal.PARENT_MISSING,
        )


def test_a_refused_reply_must_name_its_reason() -> None:
    with pytest.raises(ValueError, match="names why"):
        ProviderFilesystemReply(REQUEST_ID, ProviderFilesystemAnswer.REFUSED)


def test_only_a_workspace_io_failure_may_name_an_errno() -> None:
    with pytest.raises(ValueError, match="only a workspace I/O failure"):
        _refused(ProviderFilesystemRefusal.PARENT_MISSING, "EIO")


@pytest.mark.parametrize(
    "effect", [ProviderFilesystemEffect.READ, ProviderFilesystemEffect.WRITE]
)
def test_a_request_inside_the_lease_is_decided_under_the_workspace_scope(
    tmp_path: Path, effect: ProviderFilesystemEffect
) -> None:
    """One question per request, naming the effect, the fixed workspace scope
    and the file-call correlation of this attempt's ordinal -- never the path."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"hello")
    ledger = _Ledger()
    request = (
        _read(Path("notes.md"))
        if effect is ProviderFilesystemEffect.READ
        else _write(Path("notes.md"), b"replaced")
    )

    reply = _access(workspace, ledger=ledger).answer(request)

    assert reply.answer is ProviderFilesystemAnswer.ANSWERED
    assert ledger.refused == []
    assert ledger.decided == [
        _question(
            PermissionEffect.WORKSPACE_READ
            if effect is ProviderFilesystemEffect.READ
            else PermissionEffect.WORKSPACE_WRITE
        )
    ]


def test_a_read_the_policy_refuses_is_refused_after_the_fence_and_before_the_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"hello")
    ledger = _Ledger(policy=GRANTS_NOTHING)
    open_calls = _spying_open(monkeypatch)

    reply = _access(workspace, ledger=ledger).answer(_read(Path("notes.md")))

    assert reply == _refused(ProviderFilesystemRefusal.PERMISSION_REFUSED)
    assert ledger.decided == [_question(PermissionEffect.WORKSPACE_READ)]
    assert ledger.refused == []
    assert open_calls == []


def test_a_write_the_policy_refuses_creates_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = _Ledger(policy=GRANTS_NOTHING)

    reply = _access(workspace, ledger=ledger).answer(_write(Path("notes.md"), b"x"))

    assert reply == _refused(ProviderFilesystemRefusal.PERMISSION_REFUSED)
    assert ledger.decided == [_question(PermissionEffect.WORKSPACE_WRITE)]
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    "effect", [ProviderFilesystemEffect.READ, ProviderFilesystemEffect.WRITE]
)
def test_a_receipt_that_cannot_be_kept_moves_no_byte_and_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effect: ProviderFilesystemEffect
) -> None:
    """ADR 0020 §2: the receipt is the authorisation. A ledger that will not
    take it leaves no decision to act on -- the read opens no data descriptor,
    the write stages nothing, and the failure rises to whoever holds the
    process rather than being answered around."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"original")
    open_calls = _spying_open(monkeypatch)
    request = (
        _read(Path("notes.md"))
        if effect is ProviderFilesystemEffect.READ
        else _write(Path("notes.md"), b"replaced")
    )

    with pytest.raises(_TheLedgerIsGone):
        _access(workspace, ledger=_raising_ledger()).answer(request)

    assert open_calls == []
    assert (workspace / "notes.md").read_bytes() == b"original"
    assert list(workspace.iterdir()) == [workspace / "notes.md"]


def test_a_failure_after_the_grant_leaves_the_one_receipt_it_already_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = _Ledger()

    def raising(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "simulated staging failure")

    monkeypatch.setattr(attempt_workspace_files.os, "fsync", raising)

    reply = _access(workspace, ledger=ledger).answer(_write(Path("notes.md"), b"x"))

    assert reply.refusal is ProviderFilesystemRefusal.WORKSPACE_IO_FAILED
    assert ledger.decided == [_question(PermissionEffect.WORKSPACE_WRITE)]
    assert ledger.refused == []


def test_each_file_request_ordinal_is_its_own_question(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_bytes(b"hello")
    ledger = _Ledger()
    access = _access(workspace, ledger=ledger)

    access.answer(_read(Path("notes.md")))
    access.answer(
        ProviderFilesystemRequest(
            ProviderFilesystemEffect.READ,
            Path("notes.md"),
            ProviderFilesystemRequestId(2),
        )
    )

    assert ledger.decided == [
        _question(PermissionEffect.WORKSPACE_READ, 1),
        _question(PermissionEffect.WORKSPACE_READ, 2),
    ]


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
