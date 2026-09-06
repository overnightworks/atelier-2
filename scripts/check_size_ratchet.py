"""The size and complexity ratchet: today's oversized code may not grow.

`src/atelier2` already carries files, functions, and branchy functions past the
thresholds below. Fixing all of them is not this gate's job; holding today's
debt from growing is. `size_ratchet_baseline.toml` names every offender this
tree already carries at its current value: a path or qualified symbol over its
threshold but missing from the baseline is new debt, and one that grew past its
baseline value is growth -- both are red. An entry that no longer offends is an
orphan and is red too, so the baseline never grows quietly; shrinking a listed
offender is green and asks nothing of this file.

Follows the pattern of the duplicate ratchet in `scripts/check_architecture.py`.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_PACKAGE = "atelier2"
SOURCE_PACKAGE_DIRECTORY = "src/atelier2"

# Past this many lines a module no longer fits in one reviewing pass.
FILE_LINE_THRESHOLD = 800
# Past this many lines a function or method carries more than one decision a
# reader can hold at once.
FUNCTION_LINE_THRESHOLD = 60
# Ruff's own McCabe gate: past this branching count a function's paths no
# longer fit in a reviewer's head. atelier2 has not adopted this as a hard
# quality gate, so the ratchet only stops today's offenders from growing.
COMPLEXITY_THRESHOLD = 15

SIZE_RATCHET_BASELINE_FILE = "scripts/baselines/size_ratchet_baseline.toml"
RUFF_COMPLEXITY_RULE = "C901"
_COMPLEXITY_VALUE_PATTERN = re.compile(r"\((\d+) > \d+\)")

FunctionDefinition = ast.FunctionDef | ast.AsyncFunctionDef


class SizeRatchetError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Offender:
    """One path or qualified symbol found over its threshold today."""

    identity: str
    location: str
    value: int


@dataclass(frozen=True, slots=True)
class RatchetTable:
    """One baseline table: what it names, and the words its messages use."""

    name: str
    identity_field: str
    value_field: str
    measure_noun: str
    threshold: int


FILE_TABLE = RatchetTable("file", "path", "lines", "lines", FILE_LINE_THRESHOLD)
FUNCTION_TABLE = RatchetTable(
    "function", "qualified_name", "lines", "lines", FUNCTION_LINE_THRESHOLD
)
COMPLEXITY_TABLE = RatchetTable(
    "complexity", "qualified_name", "complexity", "complexity", COMPLEXITY_THRESHOLD
)


def _module_name(module_path: Path, source_root: Path) -> str:
    parts = module_path.relative_to(source_root).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((ROOT_PACKAGE, *parts))


def _qualified_definitions(
    node: ast.AST, prefix: str
) -> Iterator[tuple[str, FunctionDefinition]]:
    """Every function or method this node holds, under its qualified name."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified_name = f"{prefix}.{child.name}"
            if not isinstance(child, ast.ClassDef):
                yield qualified_name, child
            yield from _qualified_definitions(child, qualified_name)


def _function_length(node: FunctionDefinition) -> int:
    if node.end_lineno is None:
        raise SizeRatchetError(
            f"a function definition at line {node.lineno} carries no end line"
        )
    return node.end_lineno - node.lineno + 1


def source_functions(
    project_root: Path,
) -> tuple[tuple[str, str, FunctionDefinition], ...]:
    """Every function and method of the source package: name, path, and node."""
    source_root = project_root / SOURCE_PACKAGE_DIRECTORY
    functions: list[tuple[str, str, FunctionDefinition]] = []
    for module_path in sorted(source_root.rglob("*.py")):
        relative = module_path.relative_to(project_root).as_posix()
        module = ast.parse(
            module_path.read_text(encoding="utf-8"), filename=str(module_path)
        )
        module_name = _module_name(module_path, source_root)
        for qualified_name, node in _qualified_definitions(module, module_name):
            functions.append((qualified_name, relative, node))
    return tuple(functions)


def oversized_files(project_root: Path) -> tuple[Offender, ...]:
    source_root = project_root / SOURCE_PACKAGE_DIRECTORY
    offenders: list[Offender] = []
    for module_path in sorted(source_root.rglob("*.py")):
        relative = module_path.relative_to(project_root).as_posix()
        line_count = sum(1 for _ in module_path.open(encoding="utf-8"))
        if line_count >= FILE_LINE_THRESHOLD:
            offenders.append(Offender(relative, relative, line_count))
    return tuple(offenders)


def oversized_functions(project_root: Path) -> tuple[Offender, ...]:
    offenders: list[Offender] = []
    for qualified_name, relative, node in source_functions(project_root):
        length = _function_length(node)
        if length >= FUNCTION_LINE_THRESHOLD:
            offenders.append(
                Offender(qualified_name, f"{relative}:{node.lineno}", length)
            )
    return tuple(offenders)


def _complexity_value(message: str) -> int:
    match = _COMPLEXITY_VALUE_PATTERN.search(message)
    if match is None:
        raise SizeRatchetError(
            f"ruff's complexity message carries no reported value: {message!r}"
        )
    return int(match.group(1))


def _ruff_complexity_findings(project_root: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "ruff",
            "check",
            SOURCE_PACKAGE_DIRECTORY,
            "--select",
            RUFF_COMPLEXITY_RULE,
            "--output-format=json",
            f"--config=lint.mccabe.max-complexity={COMPLEXITY_THRESHOLD}",
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise SizeRatchetError(f"ruff check failed: {result.stderr.strip()}")
    findings: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    return findings


def complex_functions(project_root: Path) -> tuple[Offender, ...]:
    """Every function ruff's McCabe rule reports over the threshold today.

    Ruff names the finding's short name and line but not its qualified symbol,
    so its (file, definition line) is matched against the ratchet's own scan of
    the same tree, which does carry the qualified name.
    """
    functions = source_functions(project_root)
    locations = {
        (relative, node.lineno): qualified_name
        for qualified_name, relative, node in functions
    }
    resolved_root = project_root.resolve()
    offenders: list[Offender] = []
    for finding in _ruff_complexity_findings(project_root):
        filename = Path(finding["filename"]).resolve()
        relative = filename.relative_to(resolved_root).as_posix()
        row = finding["location"]["row"]
        qualified_name = locations.get((relative, row))
        if qualified_name is None:
            raise SizeRatchetError(
                f"{relative}:{row}: ruff reported a complexity finding at a line "
                "the ratchet's own function scan does not recognise"
            )
        offenders.append(
            Offender(
                qualified_name,
                f"{relative}:{row}",
                _complexity_value(finding["message"]),
            )
        )
    return tuple(offenders)


def _baseline_shape_refusal(table: RatchetTable) -> str:
    return (
        f"{SIZE_RATCHET_BASELINE_FILE}: every [[{table.name}]] names a "
        f"{table.identity_field} and a {table.value_field}"
    )


def read_baseline_table(project_root: Path, table: RatchetTable) -> dict[str, int]:
    """The identity-to-value map one baseline table already carries.

    The file is edited by hand, so a table of another shape is as likely as a
    typo in it, and reading it as one anyway would answer the ratchet with a
    crash instead of a sentence naming the file.
    """
    path = project_root / SIZE_RATCHET_BASELINE_FILE
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise SizeRatchetError(
            f"{SIZE_RATCHET_BASELINE_FILE} is not readable as TOML: {error}"
        ) from error
    entries = document.get(table.name, [])
    if not isinstance(entries, list):
        raise SizeRatchetError(_baseline_shape_refusal(table))
    baseline: dict[str, int] = {}
    for entry in entries:
        identity = entry.get(table.identity_field) if isinstance(entry, dict) else None
        value = entry.get(table.value_field) if isinstance(entry, dict) else None
        if (
            not isinstance(identity, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
        ):
            raise SizeRatchetError(_baseline_shape_refusal(table))
        if identity in baseline:
            raise SizeRatchetError(
                f"{SIZE_RATCHET_BASELINE_FILE}: [[{table.name}]] names {identity} twice"
            )
        baseline[identity] = value
    return baseline


def table_problems(
    project_root: Path, table: RatchetTable, offenders: Sequence[Offender]
) -> tuple[str, ...]:
    """Growth and new debt against one baseline table -- and its orphan entries.

    The ratchet holds in both directions on purpose: a new or grown offender is
    red because the tree grew past what the baseline already names, and a
    baseline entry whose offender is gone is red because a list that only ever
    grows stops describing anything.
    """
    baseline = read_baseline_table(project_root, table)
    current = {offender.identity: offender for offender in offenders}
    problems: list[str] = []
    for identity, offender in sorted(current.items()):
        baseline_value = baseline.get(identity)
        if baseline_value is None:
            problems.append(
                f"{offender.location}: {identity} has {offender.value} "
                f"{table.measure_noun}, over the {table.threshold} threshold and "
                f"not yet in {SIZE_RATCHET_BASELINE_FILE}"
            )
        elif offender.value > baseline_value:
            problems.append(
                f"{offender.location}: {identity} grew from {baseline_value} to "
                f"{offender.value} {table.measure_noun}; {SIZE_RATCHET_BASELINE_FILE} "
                "holds the value it may not exceed"
            )
    for identity in sorted(baseline.keys() - current.keys()):
        problems.append(
            f"{identity} no longer exceeds the {table.threshold} "
            f"{table.measure_noun} threshold: orphan baseline entry, remove it "
            f"from {SIZE_RATCHET_BASELINE_FILE}"
        )
    return tuple(problems)


def size_ratchet_problems(project_root: Path) -> tuple[str, ...]:
    categories = (
        (FILE_TABLE, oversized_files(project_root)),
        (FUNCTION_TABLE, oversized_functions(project_root)),
        (COMPLEXITY_TABLE, complex_functions(project_root)),
    )
    problems: list[str] = []
    for table, offenders in categories:
        problems.extend(
            f"{table.name}: {problem}"
            for problem in table_problems(project_root, table, offenders)
        )
    return tuple(problems)


def main() -> int:
    project_root = Path.cwd()
    try:
        problems = size_ratchet_problems(project_root)
    except (
        SizeRatchetError,
        FileNotFoundError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        print(f"Size ratchet refused: {error}", file=sys.stderr)
        return 1
    if problems:
        print(
            "size ratchet failed:\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        return 1
    print(
        "Size ratchet: file, function, and complexity baselines hold, nothing grew",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
