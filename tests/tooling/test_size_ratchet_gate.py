"""The size and complexity ratchet: today's oversized code may not grow.

The gate is driven the way CI drives it -- as its own process over a scratch
project -- because the sentence under test is what the whole tool answers, not
how it measures one function. The four ratchet sentences (new offender, growth,
shrink, orphan) are proven once against the function table, which is the
simplest offender to construct; the file and complexity tables get their own
smoke tests to prove their independent measurement path is wired correctly.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts") / "check_size_ratchet.py"
REPORT_CORRIDOR = Path("scripts") / "report_corridor.py"
DOCUMENTATION_LINES = Path("scripts") / "python_documentation_lines.py"
BASELINE = Path("scripts") / "baselines" / "size_ratchet_baseline.toml"
SOURCE_PACKAGE = Path("src") / "atelier2"
FILE_LINE_THRESHOLD = 800

LONG_FUNCTION_MODULE = "funcs.py"
LONG_FUNCTION_NAME = "long_function"
LONG_FUNCTION_QUALIFIED_NAME = f"atelier2.funcs.{LONG_FUNCTION_NAME}"

BRANCHY_MODULE = "branchy.py"
BRANCHY_FUNCTION_NAME = "branchy"
BRANCHY_QUALIFIED_NAME = f"atelier2.branchy.{BRANCHY_FUNCTION_NAME}"

BIG_MODULE = "big.py"
BIG_MODULE_PATH = str(SOURCE_PACKAGE / BIG_MODULE)

DENSIFYING_MODULE = "densifying.py"
DENSIFYING_MODULE_PATH = str(SOURCE_PACKAGE / DENSIFYING_MODULE)
GIT_IDENTITY = ("-c", "user.name=test-builder", "-c", "user.email=test-builder@invalid")


def a_function_of(name: str, total_lines: int) -> str:
    """Source text for a function measuring exactly `total_lines` end to end."""
    body_line_count = total_lines - 2
    body = "\n".join(f"    value{index} = {index}" for index in range(body_line_count))
    return f"def {name}(x: int) -> int:\n{body}\n    return x\n"


def a_branchy_function(name: str, branches: int) -> str:
    """A function whose McCabe complexity is exactly `branches` + 1."""
    lines = [f"def {name}(x: int) -> int:", "    total = 0"]
    for index in range(branches):
        lines.append(f"    if x == {index}:")
        lines.append(f"        total = total + {index}")
    lines.append("    return total")
    return "\n".join(lines) + "\n"


def a_file_of(line_count: int) -> str:
    """Source text for a module measuring exactly `line_count` lines."""
    return "\n".join(f"value_{index} = {index}" for index in range(line_count)) + "\n"


def a_module_with_documentation(function_count: int) -> str:
    """A module docstring line, a comment line, then `function_count` tiny
    two-line functions -- the shape a documented module has before it is
    compacted down to its code alone."""
    functions = "\n\n".join(
        f"def function_{index}(x: int) -> int:\n    return x + {index}"
        for index in range(function_count)
    )
    return (
        '"""Why this module exists: a tradeoff worth remembering."""\n\n'
        "# keep this guard because it protects a known invariant\n"
        f"{functions}\n"
    )


def a_module_without_documentation(function_count: int) -> str:
    """The same tiny functions as `a_module_with_documentation`, carrying no
    docstring or comment at all."""
    functions = "\n\n".join(
        f"def function_{index}(x: int) -> int:\n    return x + {index}"
        for index in range(function_count)
    )
    return f"{functions}\n"


def a_documented_function(name: str, extra_code_lines: int = 0) -> str:
    """A function with its own one-line docstring plus `extra_code_lines`
    more lines of pure code -- so deleting the whole function removes code
    and documentation together, never one without the other."""
    body = "\n".join(
        f"    value_{index} = {index}" for index in range(extra_code_lines)
    )
    body = f"{body}\n" if body else ""
    return (
        f"def {name}(x: int) -> int:\n"
        f'    """One-line docstring for {name}."""\n'
        f"{body}"
        "    return x\n"
    )


def a_function_without_documentation(name: str, extra_code_lines: int = 0) -> str:
    """The same shape as `a_documented_function`, carrying no docstring."""
    body = "\n".join(
        f"    value_{index} = {index}" for index in range(extra_code_lines)
    )
    body = f"{body}\n" if body else ""
    return f"def {name}(x: int) -> int:\n{body}    return x\n"


def a_module_of(*functions: str) -> str:
    return "\n".join(functions)


def scratch_project(
    tmp_path: Path, modules: dict[str, str], baseline: str = ""
) -> Path:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / GATE, project / GATE)
    shutil.copy2(PROJECT_ROOT / REPORT_CORRIDOR, project / REPORT_CORRIDOR)
    shutil.copy2(PROJECT_ROOT / DOCUMENTATION_LINES, project / DOCUMENTATION_LINES)
    package = project / SOURCE_PACKAGE
    package.mkdir(parents=True)
    for module, source in modules.items():
        (package / module).write_text(source, encoding="utf-8")
    (project / BASELINE).parent.mkdir()
    (project / BASELINE).write_text(baseline, encoding="utf-8")
    return project


def run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    )


def scratch_git_project(tmp_path: Path) -> Path:
    """A scratch project like `scratch_project`, but a real git repository so
    the densification signal has a base and a head revision to diff."""
    project = scratch_project(tmp_path, {})
    _git(project, "init", "--quiet")
    return project


def write_module(project: Path, name: str, source: str) -> None:
    (project / SOURCE_PACKAGE / name).write_text(source, encoding="utf-8")


def delete_module(project: Path, name: str) -> None:
    (project / SOURCE_PACKAGE / name).unlink()


def commit(project: Path, message: str) -> str:
    _git(project, "add", "-A")
    _git(project, *GIT_IDENTITY, "commit", "--quiet", "-m", message)
    return _git(project, "rev-parse", "HEAD").stdout.strip()


def run_gate_with_base(project: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--base", base],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def a_function_baseline(qualified_name: str, lines: int) -> str:
    return f'[[function]]\nqualified_name = "{qualified_name}"\nlines = {lines}\n'


def a_complexity_baseline(qualified_name: str, complexity: int) -> str:
    return f'[[complexity]]\nqualified_name = "{qualified_name}"\ncomplexity = {complexity}\n'


def a_file_baseline(path: str, lines: int) -> str:
    return f'[[file]]\npath = "{path}"\nlines = {lines}\n'


def test_a_new_offender_is_refused_with_its_location(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path, {LONG_FUNCTION_MODULE: a_function_of(LONG_FUNCTION_NAME, 60)}
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert LONG_FUNCTION_QUALIFIED_NAME in result.stderr
    assert f"{SOURCE_PACKAGE / LONG_FUNCTION_MODULE}:1" in result.stderr
    assert str(BASELINE) in result.stderr


def test_a_baseline_named_offender_at_its_baseline_value_is_quiet(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {LONG_FUNCTION_MODULE: a_function_of(LONG_FUNCTION_NAME, 60)},
        baseline=a_function_baseline(LONG_FUNCTION_QUALIFIED_NAME, 60),
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_an_offender_that_grew_past_its_baseline_value_is_refused(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {LONG_FUNCTION_MODULE: a_function_of(LONG_FUNCTION_NAME, 61)},
        baseline=a_function_baseline(LONG_FUNCTION_QUALIFIED_NAME, 60),
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "grew from 60 to 61" in result.stderr


def test_an_offender_that_shrank_but_still_offends_is_quiet(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {LONG_FUNCTION_MODULE: a_function_of(LONG_FUNCTION_NAME, 60)},
        baseline=a_function_baseline(LONG_FUNCTION_QUALIFIED_NAME, 65),
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_baseline_entry_that_no_longer_offends_is_an_orphan(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {LONG_FUNCTION_MODULE: a_function_of(LONG_FUNCTION_NAME, 10)},
        baseline=a_function_baseline(LONG_FUNCTION_QUALIFIED_NAME, 65),
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "orphan baseline entry, remove it" in result.stderr
    assert LONG_FUNCTION_QUALIFIED_NAME in result.stderr


@pytest.mark.parametrize(
    "baseline",
    [
        pytest.param(
            f'[[function]]\nqualified_name = "{LONG_FUNCTION_QUALIFIED_NAME}"\n',
            id="a value missing",
        ),
        pytest.param(
            f'[[function]]\nqualified_name = "{LONG_FUNCTION_QUALIFIED_NAME}"\n'
            'lines = "sixty"\n',
            id="a value that is not a number",
        ),
        pytest.param(
            'function = ["not a table"]\n', id="a table that is not a list of entries"
        ),
        pytest.param("[[function]\n", id="not readable as TOML"),
        pytest.param(
            a_function_baseline(LONG_FUNCTION_QUALIFIED_NAME, 60)
            + a_function_baseline(LONG_FUNCTION_QUALIFIED_NAME, 61),
            id="the same symbol named twice",
        ),
    ],
)
def test_a_malformed_baseline_is_refused_by_name(tmp_path: Path, baseline: str) -> None:
    project = scratch_project(
        tmp_path,
        {LONG_FUNCTION_MODULE: a_function_of(LONG_FUNCTION_NAME, 60)},
        baseline=baseline,
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert str(BASELINE) in result.stderr, result.stderr


def test_an_oversized_file_not_yet_in_the_baseline_is_refused(tmp_path: Path) -> None:
    project = scratch_project(tmp_path, {BIG_MODULE: a_file_of(800)})

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert BIG_MODULE_PATH in result.stderr


def test_a_baseline_named_file_at_its_baseline_value_is_quiet(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {BIG_MODULE: a_file_of(800)},
        baseline=a_file_baseline(BIG_MODULE_PATH, 800),
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_function_over_the_complexity_threshold_is_refused(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path, {BRANCHY_MODULE: a_branchy_function(BRANCHY_FUNCTION_NAME, 15)}
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert BRANCHY_QUALIFIED_NAME in result.stderr


def test_a_baseline_named_complex_function_at_its_baseline_value_is_quiet(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {BRANCHY_MODULE: a_branchy_function(BRANCHY_FUNCTION_NAME, 15)},
        baseline=a_complexity_baseline(BRANCHY_QUALIFIED_NAME, 16),
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("base_source", "head_source", "expect_red"),
    [
        pytest.param(
            a_module_of(
                a_documented_function("kept"), a_documented_function("deleted", 8)
            ),
            a_module_of(a_documented_function("kept")),
            False,
            id="deleting a whole documented function that raises the share is quiet",
        ),
        pytest.param(
            a_module_of(
                a_documented_function("kept"), a_documented_function("deleted", 8)
            ),
            a_module_of(
                a_documented_function("kept"),
                a_function_without_documentation("deleted", 8),
            ),
            True,
            id="a docstring disappears while its code stays put is red",
        ),
        pytest.param(
            a_module_of(
                a_documented_function("kept"), a_documented_function("deleted", 8)
            ),
            a_module_of(
                a_documented_function("kept"),
                a_function_without_documentation("deleted", 4),
            ),
            True,
            id="code and documentation shrink together but share still falls is red",
        ),
        pytest.param(
            a_module_of(
                a_documented_function("kept"),
                a_function_without_documentation("deleted", 8),
            ),
            a_module_of(a_documented_function("kept")),
            False,
            id="code shrinks while documentation is unchanged is quiet",
        ),
    ],
)
def test_densification_gate_reads_the_documentation_share(
    tmp_path: Path, base_source: str, head_source: str, expect_red: bool
) -> None:
    project = scratch_git_project(tmp_path)
    write_module(project, DENSIFYING_MODULE, base_source)
    base = commit(project, "base")
    write_module(project, DENSIFYING_MODULE, head_source)
    commit(project, "head")

    result = run_gate_with_base(project, base)

    if expect_red:
        assert result.returncode == 1, result.stdout + result.stderr
        assert DENSIFYING_MODULE_PATH in result.stderr
        assert "documentation share fell from" in result.stderr
        assert "disappeared" in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr


def test_a_trailing_comment_counts_as_code_and_documentation_together(
    tmp_path: Path,
) -> None:
    """Regression: a line like `return x  # why` must not count only as
    documentation and hide its own code from the census -- three functions,
    two lines of code each, one of them with a trailing comment, read as 6
    code lines and 1 documentation line, not 5 and 1."""
    project = scratch_git_project(tmp_path)
    base_source = (
        "def guarded(x: int) -> int:\n"
        "    return x  # keep this guard because it protects a known invariant\n"
        "\n"
        "def plain_one(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "def plain_two(x: int) -> int:\n"
        "    return x + 2\n"
    )
    head_source = base_source.replace(
        "    return x  # keep this guard because it protects a known invariant\n",
        "    return x\n",
    )
    write_module(project, DENSIFYING_MODULE, base_source)
    base = commit(project, "base")
    write_module(project, DENSIFYING_MODULE, head_source)
    commit(project, "head")

    result = run_gate_with_base(project, base)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "documentation share fell from 0.14 to 0.00" in result.stderr


def test_a_file_split_leaves_the_shrunken_old_file_quiet(tmp_path: Path) -> None:
    project = scratch_git_project(tmp_path)
    write_module(
        project, DENSIFYING_MODULE, a_module_with_documentation(function_count=2)
    )
    base = commit(project, "base")
    write_module(
        project, DENSIFYING_MODULE, a_module_with_documentation(function_count=1)
    )
    write_module(project, "densifying_extracted.py", a_module_with_documentation(1))
    commit(project, "split one function into its own module")

    result = run_gate_with_base(project, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_pure_rename_without_edits_is_quiet(tmp_path: Path) -> None:
    project = scratch_git_project(tmp_path)
    content = a_module_with_documentation(function_count=1)
    write_module(project, "before_rename.py", content)
    base = commit(project, "base")
    delete_module(project, "before_rename.py")
    write_module(project, "after_rename.py", content)
    commit(project, "rename without editing")

    result = run_gate_with_base(project, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_rename_combined_with_densification_is_still_red(tmp_path: Path) -> None:
    """Regression: a `git mv` must not exempt real densification. The base
    census for a renamed path is read from the path it was renamed from, so
    `git mv module.py renamed.py` plus deleted docstrings still counts
    against the file it came from, not as a brand-new, undocumented file."""
    project = scratch_git_project(tmp_path)
    base_source = a_module_of(
        a_documented_function("one", extra_code_lines=3),
        a_documented_function("two", extra_code_lines=3),
        a_documented_function("three", extra_code_lines=3),
    )
    write_module(project, "before_rename.py", base_source)
    base = commit(project, "base")
    delete_module(project, "before_rename.py")
    head_source = a_module_of(
        a_function_without_documentation("one", extra_code_lines=3),
        a_function_without_documentation("two", extra_code_lines=3),
        a_function_without_documentation("three", extra_code_lines=3),
    )
    write_module(project, "after_rename.py", head_source)
    commit(project, "rename and drop the docstrings")
    rename_status = _git(
        project, "diff", "-M", "--name-status", f"{base}...HEAD"
    ).stdout
    assert rename_status.startswith("R"), (
        f"this scenario must exercise git's own rename detection: {rename_status!r}"
    )

    result = run_gate_with_base(project, base)

    assert result.returncode == 1, result.stdout + result.stderr
    assert str(SOURCE_PACKAGE / "after_rename.py") in result.stderr
    assert "documentation share fell from" in result.stderr


def test_the_report_names_each_touched_files_distance_to_the_ceiling(
    tmp_path: Path,
) -> None:
    project = scratch_git_project(tmp_path)
    write_module(project, "touched.py", a_file_of(1))
    base = commit(project, "base")
    write_module(project, "touched.py", a_file_of(10))
    commit(project, "grows to ten lines")
    touched_path = str(SOURCE_PACKAGE / "touched.py")

    result = run_gate_with_base(project, base)

    assert result.returncode == 0, result.stdout + result.stderr
    distance = FILE_LINE_THRESHOLD - 10
    assert (
        f"{touched_path}: {distance} lines under the {FILE_LINE_THRESHOLD}-line ceiling"
        in (result.stdout)
    )


def test_an_unresolvable_base_is_refused(tmp_path: Path) -> None:
    project = scratch_git_project(tmp_path)
    write_module(project, "touched.py", a_file_of(1))
    commit(project, "base")

    result = run_gate_with_base(project, "does-not-exist-in-this-repository")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Size ratchet refused" in result.stderr
