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
BASELINE = Path("scripts") / "baselines" / "size_ratchet_baseline.toml"
SOURCE_PACKAGE = Path("src") / "atelier2"

LONG_FUNCTION_MODULE = "funcs.py"
LONG_FUNCTION_NAME = "long_function"
LONG_FUNCTION_QUALIFIED_NAME = f"atelier2.funcs.{LONG_FUNCTION_NAME}"

BRANCHY_MODULE = "branchy.py"
BRANCHY_FUNCTION_NAME = "branchy"
BRANCHY_QUALIFIED_NAME = f"atelier2.branchy.{BRANCHY_FUNCTION_NAME}"

BIG_MODULE = "big.py"
BIG_MODULE_PATH = str(SOURCE_PACKAGE / BIG_MODULE)


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


def scratch_project(
    tmp_path: Path, modules: dict[str, str], baseline: str = ""
) -> Path:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / GATE, project / GATE)
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
