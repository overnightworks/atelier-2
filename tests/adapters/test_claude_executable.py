"""The Claude executable path value, independent of any deployment.

Launching only the binary needs no credential directory, no search path, no
bubblewrap. `ClaudeSubscriptionSettings` layers those on top of this same
path invariant for the headless executor, but the invariant itself stands
alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.adapters.claude_executable import ClaudeExecutable


def test_an_executable_file_is_resolved_to_its_absolute_path(tmp_path: Path) -> None:
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert ClaudeExecutable(executable).path == executable.resolve()


def test_a_missing_path_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "absent"

    with pytest.raises(ValueError, match="executable file"):
        ClaudeExecutable(missing)


def test_a_non_executable_file_is_refused(tmp_path: Path) -> None:
    not_executable = tmp_path / "not-executable"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    not_executable.chmod(0o644)

    with pytest.raises(ValueError, match="executable file"):
        ClaudeExecutable(not_executable)
