"""What both architecture-gate suites need to drive the gate.

The gate is a script rather than a package, so a test that reads its answers
loads it by path, and a test that drives it end to end runs it inside a copy of
the real tree -- the gate reads the tree it stands in, and a scratch tree of a
few modules would fail its inventory long before the rule under test.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).parents[2]
ARCHITECTURE_SCRIPT = Path("scripts") / "check_architecture.py"
DUPLICATE_BASELINE = Path("scripts") / "baselines" / "duplicate_baseline.toml"


def load_architecture_script() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_architecture", PROJECT_ROOT / ARCHITECTURE_SCRIPT
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", project / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "src", project / "src")
    (project / ARCHITECTURE_SCRIPT.parent).mkdir()
    shutil.copy2(PROJECT_ROOT / ARCHITECTURE_SCRIPT, project / ARCHITECTURE_SCRIPT)
    (project / DUPLICATE_BASELINE.parent).mkdir()
    shutil.copy2(PROJECT_ROOT / DUPLICATE_BASELINE, project / DUPLICATE_BASELINE)
    return project


def run_gate(
    project: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ARCHITECTURE_SCRIPT)],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def append_to(project: Path, relative: str, text: str) -> None:
    path = project / relative
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def recalibrate_copied_source_module_count(project: Path) -> None:
    """Point the copied wrapper at this scratch tree's counted inventory.

    A mutation that must add or delete a module would otherwise fail the exact
    count before the invariant under test is reached.
    """

    script = load_architecture_script()
    counted = script.source_module_count(project / script.SOURCE_PACKAGE_DIRECTORY)
    copied = project / ARCHITECTURE_SCRIPT
    source = copied.read_text(encoding="utf-8")
    current = f"EXPECTED_SOURCE_MODULE_COUNT = {script.EXPECTED_SOURCE_MODULE_COUNT}"
    updated = f"EXPECTED_SOURCE_MODULE_COUNT = {counted}"
    assert source.count(current) == 1
    copied.write_text(source.replace(current, updated, 1), encoding="utf-8")
