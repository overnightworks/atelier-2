"""Core test modules import no adapter (AGENTS.md "Tests").

A core unit test that imports `atelier2.adapters` -- at module level or inside
a function -- ties a domain, application, or API test to one adapter's
implementation detail. `core_test_import_baseline.toml` names how many test
modules each core directory still does this today; growth turns the gate red,
and lowering a count is recorded only by running this script's own
`--write-baseline` mode, never by hand-editing the file.

Follows the pattern of the size and complexity ratchet in
`scripts/check_size_ratchet.py`.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

CORE_TEST_DIRECTORIES: tuple[str, ...] = (
    "tests/domain",
    "tests/application",
    "tests/api",
)
ADAPTER_PACKAGE = "atelier2.adapters"
_ADAPTER_PACKAGE_PARENT, _, _ADAPTER_PACKAGE_LEAF = ADAPTER_PACKAGE.rpartition(".")

CORE_TEST_IMPORT_BASELINE_FILE = "scripts/baselines/core_test_import_baseline.toml"
BASELINE_TABLE_NAME = "directory"
BASELINE_PATH_FIELD = "path"
BASELINE_COUNT_FIELD = "adapter_importing_test_modules"


class CoreTestImportError(Exception):
    pass


def _is_adapter_import(module_name: str) -> bool:
    return module_name == ADAPTER_PACKAGE or module_name.startswith(
        f"{ADAPTER_PACKAGE}."
    )


def _imports_adapter_package_by_name(node: ast.ImportFrom) -> bool:
    """`from atelier2 import adapters` names the package through its parent,
    not through `node.module`, so the imported names need their own check."""
    return node.module == _ADAPTER_PACKAGE_PARENT and any(
        alias.name == _ADAPTER_PACKAGE_LEAF for alias in node.names
    )


def _imports_adapter_package(module: ast.Module) -> bool:
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            if any(_is_adapter_import(alias.name) for alias in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (
                _is_adapter_import(node.module)
                or _imports_adapter_package_by_name(node)
            )
        ):
            return True
    return False


def adapter_importing_test_module_count(directory: Path) -> int:
    """How many `test_*.py` modules directly under `directory` import an adapter."""
    count = 0
    for module_path in sorted(directory.glob("test_*.py")):
        module = ast.parse(
            module_path.read_text(encoding="utf-8"), filename=str(module_path)
        )
        if _imports_adapter_package(module):
            count += 1
    return count


def current_counts(project_root: Path) -> dict[str, int]:
    return {
        directory: adapter_importing_test_module_count(project_root / directory)
        for directory in CORE_TEST_DIRECTORIES
    }


def _baseline_shape_refusal() -> str:
    named_directories = ", ".join(CORE_TEST_DIRECTORIES)
    return (
        f"{CORE_TEST_IMPORT_BASELINE_FILE}: every [[{BASELINE_TABLE_NAME}]] names "
        f"one of {named_directories} as {BASELINE_PATH_FIELD} and an integer "
        f"{BASELINE_COUNT_FIELD}"
    )


def read_baseline(project_root: Path) -> dict[str, int]:
    """The per-directory count `core_test_import_baseline.toml` already carries.

    The file is edited only through `--write-baseline`, so a table of another
    shape is a mistake worth naming rather than a crash to chase.
    """
    path = project_root / CORE_TEST_IMPORT_BASELINE_FILE
    with path.open("rb") as handle:
        try:
            document = tomllib.load(handle)
        except tomllib.TOMLDecodeError as error:
            raise CoreTestImportError(
                f"{CORE_TEST_IMPORT_BASELINE_FILE} is not readable as TOML: {error}"
            ) from error
    entries = document.get(BASELINE_TABLE_NAME, [])
    if not isinstance(entries, list):
        raise CoreTestImportError(_baseline_shape_refusal())
    baseline: dict[str, int] = {}
    for entry in entries:
        directory = entry.get(BASELINE_PATH_FIELD) if isinstance(entry, dict) else None
        count = entry.get(BASELINE_COUNT_FIELD) if isinstance(entry, dict) else None
        if (
            not isinstance(directory, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
        ):
            raise CoreTestImportError(_baseline_shape_refusal())
        if directory not in CORE_TEST_DIRECTORIES:
            raise CoreTestImportError(
                f"{CORE_TEST_IMPORT_BASELINE_FILE}: {directory} is not a core test "
                f"directory this gate measures ({', '.join(CORE_TEST_DIRECTORIES)})"
            )
        if directory in baseline:
            raise CoreTestImportError(
                f"{CORE_TEST_IMPORT_BASELINE_FILE}: names {directory} twice"
            )
        baseline[directory] = count
    missing = [
        directory for directory in CORE_TEST_DIRECTORIES if directory not in baseline
    ]
    if missing:
        raise CoreTestImportError(
            f"{CORE_TEST_IMPORT_BASELINE_FILE}: missing an entry for "
            f"{', '.join(missing)}"
        )
    return baseline


def core_test_import_problems(project_root: Path) -> tuple[str, ...]:
    baseline = read_baseline(project_root)
    counts = current_counts(project_root)
    problems: list[str] = []
    for directory in CORE_TEST_DIRECTORIES:
        baseline_count = baseline[directory]
        current_count = counts[directory]
        if current_count > baseline_count:
            problems.append(
                f"{directory}: adapter-importing test modules grew from "
                f"{baseline_count} to {current_count}; "
                f"{CORE_TEST_IMPORT_BASELINE_FILE} holds the count it may not exceed"
            )
    return tuple(problems)


def _write_baseline_refusal(raised_counts: Sequence[tuple[str, int, int]]) -> str:
    raised = "; ".join(
        f"{directory} from {old_count} to {new_count}"
        for directory, old_count, new_count in raised_counts
    )
    return (
        f"--write-baseline would raise {raised}; the baseline only ever lowers "
        "through this script, so remove the adapter import first"
    )


def write_baseline(project_root: Path) -> None:
    """Record today's counts, refusing to raise any of them.

    The baseline only ever lowers through this script (never by hand-editing
    the file), so a directory whose count would go up here is a mistake --
    growth belongs to the check path's refusal, not to this one.
    """
    baseline = read_baseline(project_root)
    counts = current_counts(project_root)
    raised_counts = [
        (directory, baseline[directory], counts[directory])
        for directory in CORE_TEST_DIRECTORIES
        if counts[directory] > baseline[directory]
    ]
    if raised_counts:
        raise CoreTestImportError(_write_baseline_refusal(raised_counts))
    entries = "\n".join(
        f"[[{BASELINE_TABLE_NAME}]]\n"
        f'{BASELINE_PATH_FIELD} = "{directory}"\n'
        f"{BASELINE_COUNT_FIELD} = {counts[directory]}\n"
        for directory in CORE_TEST_DIRECTORIES
    )
    (project_root / CORE_TEST_IMPORT_BASELINE_FILE).write_text(
        entries, encoding="utf-8"
    )


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check that no core test directory carries more adapter-importing "
            f"test modules than {CORE_TEST_IMPORT_BASELINE_FILE} allows."
        )
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            f"rewrite {CORE_TEST_IMPORT_BASELINE_FILE} with today's counts "
            "instead of checking them"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    project_root = Path.cwd()
    if arguments.write_baseline:
        try:
            write_baseline(project_root)
        except (CoreTestImportError, FileNotFoundError) as error:
            print(f"Core test import ratchet refused: {error}", file=sys.stderr)
            return 1
        print(f"Wrote {CORE_TEST_IMPORT_BASELINE_FILE}.", flush=True)
        return 0
    try:
        problems = core_test_import_problems(project_root)
    except (CoreTestImportError, FileNotFoundError) as error:
        print(f"Core test import ratchet refused: {error}", file=sys.stderr)
        return 1
    if problems:
        print(
            "core test import ratchet failed:\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        return 1
    print(
        "Core test import ratchet: domain, application, and api hold their "
        "adapter-importing test module baseline",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
