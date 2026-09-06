"""The core test import ratchet: a core test directory may not gain more
adapter-importing test modules than its baseline already names.

The gate is driven the way CI drives it -- as its own process over a scratch
project -- because the sentence under test is what the whole tool answers, not
how it parses one import. `tests/application` is the simplest directory to
construct scenarios for; the other two core directories get one smoke test
each to prove their independent counting path is wired correctly.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts") / "check_core_test_imports.py"
BASELINE = Path("scripts") / "baselines" / "core_test_import_baseline.toml"
CORE_TEST_DIRECTORIES = ("tests/domain", "tests/application", "tests/api")

ADAPTER_IMPORTING_MODULE = (
    "from atelier2.adapters.yaml_workflows import parse_workflow_document\n"
    "\n\ndef test_something() -> None:\n    parse_workflow_document\n"
)
ADAPTER_FREE_MODULE = "def test_something() -> None:\n    pass\n"
IMPORT_INSIDE_FUNCTION_MODULE = (
    "def test_something() -> None:\n"
    "    from atelier2.adapters.yaml_workflows import parse_workflow_document\n"
    "\n    parse_workflow_document\n"
)
IMPORT_ADAPTERS_VIA_PARENT_PACKAGE_MODULE = (
    "from atelier2 import adapters\n\n\ndef test_something() -> None:\n    adapters\n"
)


def a_baseline(counts: dict[str, int]) -> str:
    return "\n".join(
        f'[[directory]]\npath = "{directory}"\nadapter_importing_test_modules = '
        f"{counts.get(directory, 0)}\n"
        for directory in CORE_TEST_DIRECTORIES
    )


def scratch_project(tmp_path: Path, modules: dict[str, str], baseline: str) -> Path:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / GATE, project / GATE)
    for directory in CORE_TEST_DIRECTORIES:
        (project / directory).mkdir(parents=True)
    for relative_path, source in modules.items():
        (project / relative_path).write_text(source, encoding="utf-8")
    (project / BASELINE).parent.mkdir()
    (project / BASELINE).write_text(baseline, encoding="utf-8")
    return project


def run_gate(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), *arguments],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def test_a_new_adapter_importing_module_over_baseline_is_refused(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_IMPORTING_MODULE},
        baseline=a_baseline({"tests/application": 0}),
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tests/application" in result.stderr
    assert "grew from 0 to 1" in result.stderr
    assert str(BASELINE) in result.stderr


def test_an_import_inside_a_function_body_counts_too(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": IMPORT_INSIDE_FUNCTION_MODULE},
        baseline=a_baseline({"tests/application": 0}),
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "grew from 0 to 1" in result.stderr


def test_importing_the_adapters_package_via_its_parent_module_counts_too(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": IMPORT_ADAPTERS_VIA_PARENT_PACKAGE_MODULE},
        baseline=a_baseline({"tests/application": 0}),
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "grew from 0 to 1" in result.stderr


def test_a_count_at_its_baseline_value_is_quiet(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_IMPORTING_MODULE},
        baseline=a_baseline({"tests/application": 1}),
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_count_that_shrank_below_baseline_is_quiet(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_FREE_MODULE},
        baseline=a_baseline({"tests/application": 1}),
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_baseline_naming_an_unknown_directory_is_refused(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_FREE_MODULE},
        baseline=a_baseline({"tests/application": 0})
        + '\n[[directory]]\npath = "tests/crash"\nadapter_importing_test_modules = 0\n',
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tests/crash" in result.stderr
    assert "not a core test directory" in result.stderr


def test_a_baseline_missing_a_core_directory_is_refused(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_FREE_MODULE},
        baseline='[[directory]]\npath = "tests/domain"\nadapter_importing_test_modules = 0\n',
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "missing an entry for" in result.stderr
    assert "tests/application" in result.stderr


def test_a_malformed_baseline_is_refused_by_name(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_FREE_MODULE},
        baseline="[[directory]\n",
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert str(BASELINE) in result.stderr


def test_write_baseline_records_a_lowered_count(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_FREE_MODULE},
        baseline=a_baseline({"tests/application": 1}),
    )

    write_result = run_gate(project, "--write-baseline")
    check_result = run_gate(project)

    assert write_result.returncode == 0, write_result.stdout + write_result.stderr
    assert "adapter_importing_test_modules = 0" in (project / BASELINE).read_text(
        encoding="utf-8"
    )
    assert check_result.returncode == 0, check_result.stdout + check_result.stderr


def test_write_baseline_refuses_to_raise_a_count(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"tests/application/test_one.py": ADAPTER_IMPORTING_MODULE},
        baseline=a_baseline({"tests/application": 0}),
    )

    write_result = run_gate(project, "--write-baseline")

    assert write_result.returncode == 1, write_result.stdout + write_result.stderr
    assert "tests/application from 0 to 1" in write_result.stderr
    assert "adapter_importing_test_modules = 0" in (project / BASELINE).read_text(
        encoding="utf-8"
    )


def test_domain_and_api_directories_are_measured_independently(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {
            "tests/domain/test_one.py": ADAPTER_IMPORTING_MODULE,
            "tests/api/test_one.py": ADAPTER_IMPORTING_MODULE,
        },
        baseline=a_baseline({"tests/domain": 0, "tests/api": 0}),
    )

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "grew from 0 to 1" in result.stderr
    assert "tests/domain" in result.stderr
    assert "tests/api" in result.stderr
