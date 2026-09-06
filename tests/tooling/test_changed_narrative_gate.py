"""The changed-narrative gate only judges newly added Python narrative."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts") / "check_changed_narrative.py"
DIFF_READER = Path("scripts") / "report_corridor.py"
GIT_IDENTITY = ("-c", "user.name=test-builder", "-c", "user.email=test-builder@invalid")


def scratch_repository(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / GATE, project / GATE)
    shutil.copy2(PROJECT_ROOT / DIFF_READER, project / DIFF_READER)
    _git(project, "init", "--quiet")
    return project


def _git(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=project, check=True, capture_output=True, text=True
    )


def write_source(
    project: Path, content: str, relative_path: Path = Path("src/example.py")
) -> None:
    source_path = project / relative_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(content, encoding="utf-8")


def commit(project: Path, message: str) -> str:
    _git(project, "add", "-A")
    _git(project, *GIT_IDENTITY, "commit", "--quiet", "-m", message)
    return _git(project, "rev-parse", "HEAD").stdout.strip()


def run_gate(
    project: Path, base: str, *, head: str = "HEAD"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--base", base, "--head", head],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def test_added_comment_with_issue_number_is_rejected(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_source(project, "value = 1\n")
    base = commit(project, "base")
    write_source(project, "# Follow-up #1305\nvalue = 1\n")
    commit(project, "head")

    result = run_gate(project, base)

    assert result.returncode == 1
    assert "src/example.py:1: # Follow-up #1305" in result.stdout
    assert "AGENTS.md forbids" in result.stdout


def test_issue_number_in_a_string_literal_is_allowed(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_source(project, "value = 1\n")
    base = commit(project, "base")
    write_source(project, 'value = "#1305"\n')
    commit(project, "head")

    result = run_gate(project, base)

    assert result.returncode == 0
    assert result.stdout == ""


def test_decision_owner_reference_is_allowed(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_source(project, "value = 1\n")
    base = commit(project, "base")
    write_source(project, "# docs/decisions/2026-09-06.md\nvalue = 1\n")
    commit(project, "head")

    result = run_gate(project, base)

    assert result.returncode == 0
    assert result.stdout == ""


def test_uses_the_specified_head_instead_of_dirty_working_tree(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_source(project, "value = 1\n")
    base = commit(project, "base")
    write_source(project, "# Follow-up #1305\nvalue = 1\n")
    head = commit(project, "head")
    write_source(project, "value = 2\n")

    result = run_gate(project, base, head=head)

    assert result.returncode == 1
    assert "src/example.py:1: # Follow-up #1305" in result.stdout


def test_utf8_quoted_patch_path_is_reported(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    base = commit(project, "base")
    relative_path = Path("src/narrative-ä\tstory.py")
    write_source(project, "# Follow-up #1305\n", relative_path)
    commit(project, "head")

    result = run_gate(project, base)

    assert result.returncode == 1
    assert f"{relative_path}:1: # Follow-up #1305" in result.stdout


def test_unchanged_legacy_narrative_is_ignored(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_source(project, "# Follow-up #1305\nvalue = 1\n")
    base = commit(project, "base")
    write_source(project, "# Follow-up #1305\nvalue = 2\n")
    commit(project, "head")

    result = run_gate(project, base)

    assert result.returncode == 0
    assert result.stdout == ""


def test_added_docstring_line_with_date_is_rejected(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_source(project, "value = 1\n")
    base = commit(project, "base")
    write_source(project, '"""Updated 2026-09-06."""\nvalue = 1\n')
    commit(project, "head")

    result = run_gate(project, base)

    assert result.returncode == 1
    assert 'src/example.py:1: """Updated 2026-09-06."""' in result.stdout
