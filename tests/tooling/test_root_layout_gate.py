"""The root layout gate: a file the allowlist does not name is refused with its home."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.tooling.checkout_test_support import (
    MISSING_CHECKOUT_REASON,
    directory_is_a_git_checkout,
)

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts") / "check_root_layout.py"


def load_gate() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_root_layout", PROJECT_ROOT / GATE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_an_allowlisted_root_passes() -> None:
    gate = load_gate()
    allowlist = gate.RootAllowlist(frozenset({"README.md"}), frozenset({"src"}))

    problems = gate.root_layout_problems(
        ["README.md", "src/package/module.py"], allowlist
    )

    assert problems == ()


def test_a_stray_baseline_is_refused_with_where_it_belongs() -> None:
    gate = load_gate()
    allowlist = gate.RootAllowlist(frozenset({"README.md"}), frozenset({"src"}))

    problems = gate.root_layout_problems(
        ["README.md", "size_ratchet_baseline.toml", "src/module.py"], allowlist
    )

    assert problems == (
        (
            "size_ratchet_baseline.toml: a baseline belongs next to the check that "
            "reads it: scripts/baselines/"
        ),
    )


def test_a_stray_directory_is_refused() -> None:
    gate = load_gate()
    allowlist = gate.RootAllowlist(frozenset({"README.md"}), frozenset({"src"}))

    problems = gate.root_layout_problems(["misc/anything.txt"], allowlist)

    assert problems == (
        (
            "misc/: a new top-level directory needs a named owner and an entry in "
            "scripts/check_root_layout.py"
        ),
    )


def test_the_checkout_predicate_is_false_for_a_plain_directory_and_true_after_git_init(
    tmp_path: Path,
) -> None:
    assert directory_is_a_git_checkout(tmp_path) is False

    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True
    )

    assert directory_is_a_git_checkout(tmp_path) is True


@pytest.mark.skipif(
    not directory_is_a_git_checkout(PROJECT_ROOT),
    reason=MISSING_CHECKOUT_REASON,
)
def test_the_repository_root_passes_the_gate() -> None:
    result = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
