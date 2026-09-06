"""What `AttemptWorkspaceFileAccess` grants inside one attempt's real lease,
and what it refuses before ever reaching outside it.

Every scenario runs against a real temporary directory tree: the fence is a
descriptor discipline over actual `openat` calls, so a filesystem fake could
only assert that the adapter believes its own abstraction, never that a
symlink, a swapped directory, or an escaping path is truly refused.
"""

from __future__ import annotations

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
    [Path("notes.md"), None],
    ids=["relative", "lease-absolute"],
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


def test_a_nested_relative_path_inside_the_lease_is_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "deep.txt").write_bytes(b"deep bytes")

    outcome = _access(workspace).describe(_read(Path("sub/deep.txt")))

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
        AttemptWorkspaceFileRefusal.PATH_LEFT_THE_LEASE,
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

    outcome = _access(workspace).describe(_read(requested))

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is scenario.refusal


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


def test_a_path_component_swapped_for_a_symlink_after_the_lease_is_still_refused(
    tmp_path: Path,
) -> None:
    """The lease was taken while `sub` was a real directory; a peer later
    replaces it with a symlink reaching outside. Because every step of the
    walk is opened fresh with `O_NOFOLLOW`, the swap that happened after the
    lease is caught exactly like a symlink that was there from the start."""

    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "file.txt").write_bytes(b"real content")
    lease = _lease(workspace)

    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "file.txt").write_bytes(b"sentinel secret")
    shutil.rmtree(workspace / "sub")
    (workspace / "sub").symlink_to(sentinel)

    outcome = AttemptWorkspaceFileAccess(lease, A_READ_CEILING).describe(
        _read(Path("sub/file.txt"))
    )

    assert outcome.reply == ProviderFilesystemReply(
        REQUEST_ID, ProviderFilesystemAnswer.REFUSED
    )
    assert outcome.refusal is AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK
    assert (sentinel / "file.txt").read_bytes() == b"sentinel secret"


def test_a_file_wider_than_the_injected_ceiling_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "big.bin").write_bytes(b"x" * 10)

    outcome = _access(workspace, maximum_read_bytes=5).describe(_read(Path("big.bin")))

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
    """Record every real name `os.open` is asked to open, still opening it."""

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


def test_a_pure_escape_opens_nothing_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "secret.txt").write_bytes(b"do not read me")
    opened_names = _spying_open(monkeypatch)

    outcome = _access(workspace).describe(_read(Path("../sentinel/secret.txt")))

    assert outcome.reply.answer is ProviderFilesystemAnswer.REFUSED
    assert opened_names == []


def test_a_symlink_escape_never_opens_the_directory_it_points_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "secret.txt").write_bytes(b"do not read me")
    (workspace / "escape").symlink_to(sentinel)
    opened_names = _spying_open(monkeypatch)

    outcome = _access(workspace).describe(_read(Path("escape/secret.txt")))

    assert outcome.reply.answer is ProviderFilesystemAnswer.REFUSED
    assert outcome.refusal is AttemptWorkspaceFileRefusal.PATH_NAMED_A_SYMLINK
    assert "secret.txt" not in opened_names


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
