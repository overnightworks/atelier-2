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

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from report_corridor import CorridorError, git_diff_lines

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


@dataclass(frozen=True, slots=True)
class LineCensus:
    """One module's physical lines, split into code and documentation.

    Documentation is a docstring expression's own lines (from `ast`, at the
    canonical module/class/function docstring position) plus any line
    carrying a `#` comment (from `tokenize`); every other non-blank line
    counts as code.
    """

    code_lines: int
    documentation_lines: int


def _docstring_line_numbers(tree: ast.Module) -> set[int]:
    line_numbers: set[int] = set()
    definition_nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, definition_nodes) or not node.body:
            continue
        statement = node.body[0]
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
            and statement.end_lineno is not None
        ):
            line_numbers.update(range(statement.lineno, statement.end_lineno + 1))
    return line_numbers


def _comment_line_numbers(source: str) -> set[int]:
    return {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }


def census(source: str) -> LineCensus:
    """A module's code and documentation line counts, from its own text."""
    tree = ast.parse(source)
    documentation_line_numbers = _docstring_line_numbers(tree) | _comment_line_numbers(
        source
    )
    code_line_count = sum(
        1
        for line_number, line in enumerate(source.splitlines(), start=1)
        if line_number not in documentation_line_numbers and line.strip()
    )
    return LineCensus(code_line_count, len(documentation_line_numbers))


def _blob_content(project_root: Path, revision: str, relative_path: str) -> str | None:
    """A file's text at one revision, or None when that path does not exist
    there -- a file the diff added or removed between base and head."""
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else None


def _changed_source_paths(project_root: Path, base: str, head: str) -> tuple[str, ...]:
    prefix = f"{SOURCE_PACKAGE_DIRECTORY}/"
    return tuple(
        sorted(
            path
            for path in git_diff_lines(
                project_root,
                base,
                head,
                "--name-only",
                rename_detection="--no-renames",
            )
            if path.strip() and path.startswith(prefix) and path.endswith(".py")
        )
    )


def _census_or_raise(relative_path: str, revision: str, content: str) -> LineCensus:
    try:
        return census(content)
    except (SyntaxError, tokenize.TokenError) as error:
        raise SizeRatchetError(
            f"{relative_path} at {revision} is not readable as Python: {error}"
        ) from error


def _census_at_revision(
    project_root: Path, revision: str, relative_path: str
) -> LineCensus:
    """A path's line census at one revision -- (0, 0) when that revision does
    not carry it, so a brand-new file can never look like it lost
    documentation it never had a chance to have."""
    content = _blob_content(project_root, revision, relative_path)
    if content is None:
        return LineCensus(0, 0)
    return _census_or_raise(relative_path, revision, content)


@dataclass(frozen=True, slots=True)
class ChangedFileReport:
    """One touched source file: its densification problem, if any, and how
    many lines it sits under (positive) or over (negative) the file ceiling."""

    path: str
    problem: str | None
    ceiling_distance: int


def densification_report(
    project_root: Path, base: str, head: str
) -> tuple[ChangedFileReport, ...]:
    """Every touched source file's distance to the file ceiling, and red only
    where code grew while comment or docstring lines shrank in the same file
    -- the shape of change that hides growth behind deleted explanation
    instead of an honest split, independent of where the file ends up
    relative to the ceiling.

    A file the diff only deleted from, or only moved without editing, cannot
    turn up red here: its code line count did not grow, or its base census
    reads as (0, 0) because the path is new -- either way the two conditions
    below cannot both hold.
    """
    reports: list[ChangedFileReport] = []
    for relative_path in _changed_source_paths(project_root, base, head):
        head_content = _blob_content(project_root, head, relative_path)
        if head_content is None:
            continue
        head_census = _census_or_raise(relative_path, head, head_content)
        base_census = _census_at_revision(project_root, base, relative_path)
        code_delta = head_census.code_lines - base_census.code_lines
        documentation_delta = (
            head_census.documentation_lines - base_census.documentation_lines
        )
        problem = None
        if code_delta > 0 and documentation_delta < 0:
            problem = (
                f"{relative_path}: code grew by {code_delta} lines while "
                f"comment and docstring lines shrank by {-documentation_delta} lines"
            )
        line_count = len(head_content.splitlines())
        reports.append(
            ChangedFileReport(relative_path, problem, FILE_LINE_THRESHOLD - line_count)
        )
    return tuple(reports)


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


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """`--base` is optional: without it the file, function, and complexity
    ratchets run exactly as they did before the densification signal existed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=None,
        help=(
            "a revision to diff against --head for the densification signal "
            "-- a touched file whose code lines grew while its comment or "
            "docstring lines shrank; omitted, only the file, function, and "
            "complexity ratchets run"
        ),
    )
    parser.add_argument(
        "--head", default="HEAD", help="the range's head revision (default: HEAD)"
    )
    return parser.parse_args(argv)


def _ceiling_distance_line(report: ChangedFileReport) -> str:
    state = "under" if report.ceiling_distance >= 0 else "over"
    return (
        f"{report.path}: {abs(report.ceiling_distance)} lines {state} the "
        f"{FILE_LINE_THRESHOLD}-line ceiling"
    )


def main() -> int:
    project_root = Path.cwd()
    arguments = _arguments()
    try:
        problems = list(size_ratchet_problems(project_root))
        if arguments.base is not None:
            file_reports = densification_report(
                project_root, arguments.base, arguments.head
            )
            for report in file_reports:
                print(_ceiling_distance_line(report), flush=True)
            problems.extend(
                f"densification: {report.problem}"
                for report in file_reports
                if report.problem is not None
            )
    except (
        SizeRatchetError,
        CorridorError,
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
