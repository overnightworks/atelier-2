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
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from python_documentation_lines import (
    LineSlice,
    comment_line_slices,
    docstring_line_slices,
)

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
    """One module's physical lines: how many carry documentation (a docstring
    expression at its canonical position, or a `#` comment) and how many
    carry code. A line can be both -- a statement with a trailing comment, or
    a one-line function whose header precedes its docstring -- so the two
    counts are independent, not a partition of the file's line count.
    """

    code_lines: int
    documentation_lines: int


def _has_code_outside_documentation(
    line: str, *documentation_slices: LineSlice | None
) -> bool:
    covered = [False] * len(line)
    for line_slice in documentation_slices:
        if line_slice is None:
            continue
        for column in range(
            max(line_slice.start_column, 0), min(line_slice.end_column, len(line))
        ):
            covered[column] = True
    return any(
        character.strip() and not is_covered
        for character, is_covered in zip(line, covered)
    )


def census(source: str) -> LineCensus:
    """A module's code and documentation line counts, from its own text --
    built on the same docstring and comment slices `check_changed_narrative.py`
    reads, so both gates agree on what documentation is."""
    tree = ast.parse(source)
    lines = source.splitlines()
    docstring_slices = docstring_line_slices(lines, tree)
    comment_slices = comment_line_slices(source)
    documentation_line_numbers = docstring_slices.keys() | comment_slices.keys()
    code_line_count = 0
    for line_number, line in enumerate(lines, start=1):
        docstring_slice = docstring_slices.get(line_number)
        comment_slice = comment_slices.get(line_number)
        if docstring_slice is None and comment_slice is None:
            if line.strip():
                code_line_count += 1
        elif _has_code_outside_documentation(line, docstring_slice, comment_slice):
            code_line_count += 1
    return LineCensus(code_line_count, len(documentation_line_numbers))


def _blob_exists(project_root: Path, revision: str, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}:{relative_path}"],
        cwd=project_root,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _blob_content(project_root: Path, revision: str, relative_path: str) -> str | None:
    """A file's text at one revision, or None when that path does not exist
    there -- a file the diff added or removed between base and head. Once
    existence is confirmed, a `git show` failure is a real error, not a
    missing path, and is raised rather than read as a silent exemption."""
    if not _blob_exists(project_root, revision, relative_path):
        return None
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=project_root,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SizeRatchetError(
            f"could not read {relative_path} at {revision}: {result.stderr.strip()}"
        )
    return result.stdout


@dataclass(frozen=True, slots=True)
class ChangedPath:
    """One file the diff touched: its path at head, and the path git's
    rename detection paired it with at base -- the same as its head path
    unless the diff renamed it."""

    head_path: str
    base_path: str


def _changed_source_paths(
    project_root: Path, base: str, head: str
) -> tuple[ChangedPath, ...]:
    """Every touched path under the source package, base path included, so a
    rename's base-side census reads the file it was renamed from rather than
    (0, 0) -- otherwise a `git mv` plus any rewrite would read as a brand-new
    file no matter what the rewrite did.

    Read with `-z`: git quotes a path in its usual, newline-terminated
    `--name-status` output whenever it carries a non-ASCII or otherwise
    unsafe byte, and a quoted path would fail the prefix check below and be
    silently dropped. A NUL-terminated record has nothing to quote, so
    there is no escaping to decode and no second decoder to keep in step
    with check_changed_narrative.py's own.
    """
    result = subprocess.run(
        ["git", "diff", "-M", "-z", "--name-status", f"{base}...{head}"],
        cwd=project_root,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SizeRatchetError(f"git diff failed: {result.stderr.strip()}")
    prefix = f"{SOURCE_PACKAGE_DIRECTORY}/"
    fields = [field for field in result.stdout.split("\0") if field]
    changed: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if status.startswith("R"):
            base_path, head_path = fields[index + 1], fields[index + 2]
            index += 3
        else:
            base_path = head_path = fields[index + 1]
            index += 2
        if head_path.startswith(prefix) and head_path.endswith(".py"):
            changed.append(ChangedPath(head_path, base_path))
    return tuple(sorted(changed, key=lambda changed_path: changed_path.head_path))


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
    not carry it, so a genuinely new file can never look like it lost
    documentation it never had a chance to have."""
    content = _blob_content(project_root, revision, relative_path)
    if content is None:
        return LineCensus(0, 0)
    return _census_or_raise(relative_path, revision, content)


def _documentation_share(line_census: LineCensus) -> float:
    total = line_census.code_lines + line_census.documentation_lines
    return line_census.documentation_lines / total if total else 0.0


def _sum_censuses(censuses: Iterable[LineCensus]) -> LineCensus:
    code_lines = documentation_lines = 0
    for line_census in censuses:
        code_lines += line_census.code_lines
        documentation_lines += line_census.documentation_lines
    return LineCensus(code_lines, documentation_lines)


def _is_densifying(base_census: LineCensus, head_census: LineCensus) -> bool:
    """True where documentation lines disappeared and, with them, the
    documentation share fell -- independent of size, and independent of the
    sign of the code-line change. Deleting a documented function whole can
    raise the share even as it removes documentation, and is quiet; deleting
    only the explanation while its code stays put cannot raise the share,
    and is exactly the shape a stealth compaction takes."""
    documentation_delta = (
        head_census.documentation_lines - base_census.documentation_lines
    )
    if documentation_delta >= 0:
        return False
    return _documentation_share(head_census) < _documentation_share(base_census)


def _densification_message(
    path: str, base_census: LineCensus, head_census: LineCensus
) -> str:
    documentation_delta = (
        head_census.documentation_lines - base_census.documentation_lines
    )
    return (
        f"{path}: documentation share fell from "
        f"{_documentation_share(base_census):.4f} to "
        f"{_documentation_share(head_census):.4f} as "
        f"{-documentation_delta} of its own comment or docstring lines disappeared"
    )


@dataclass(frozen=True, slots=True)
class ChangedFileReport:
    """One touched source file: its densification problem, if any, and how
    many lines it sits under (positive) or over (negative) the file ceiling."""

    path: str
    problem: str | None
    ceiling_distance: int


@dataclass(frozen=True, slots=True)
class _TouchedFile:
    path: str
    base_census: LineCensus
    head_census: LineCensus
    head_line_count: int


def _touched_files(
    project_root: Path, base: str, head: str
) -> tuple[_TouchedFile, ...]:
    touched: list[_TouchedFile] = []
    for changed_path in _changed_source_paths(project_root, base, head):
        head_content = _blob_content(project_root, head, changed_path.head_path)
        if head_content is None:
            continue
        head_census = _census_or_raise(changed_path.head_path, head, head_content)
        base_census = _census_at_revision(project_root, base, changed_path.base_path)
        touched.append(
            _TouchedFile(
                changed_path.head_path,
                base_census,
                head_census,
                len(head_content.splitlines()),
            )
        )
    return tuple(touched)


def densification_report(
    project_root: Path, base: str, head: str
) -> tuple[ChangedFileReport, ...]:
    """Every touched source file's distance to the file ceiling, plus a
    problem for each file that lost documentation of its own -- but only
    once the diff as a whole is densifying.

    The verdict is read from the diff's summed censuses, not any one file's
    own: a split or a rename moves lines between files without shrinking
    the diff's own total, so the file that lines moved out of cannot trip
    this alone, and the file they moved into only adds to the total. Naming
    each file that lost documentation, once the whole diff is red, keeps the
    report pointing at the actual loss rather than only the aggregate.
    """
    touched = _touched_files(project_root, base, head)
    diff_base = _sum_censuses(entry.base_census for entry in touched)
    diff_head = _sum_censuses(entry.head_census for entry in touched)
    diff_is_densifying = _is_densifying(diff_base, diff_head)

    reports: list[ChangedFileReport] = []
    for entry in touched:
        problem = None
        file_documentation_delta = (
            entry.head_census.documentation_lines
            - entry.base_census.documentation_lines
        )
        if diff_is_densifying and file_documentation_delta < 0:
            problem = _densification_message(
                entry.path, entry.base_census, entry.head_census
            )
        reports.append(
            ChangedFileReport(
                entry.path, problem, FILE_LINE_THRESHOLD - entry.head_line_count
            )
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
    """Without `--base`, only the file, function, and complexity ratchets run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=None,
        help=(
            "a revision to diff against --head for the densification signal "
            "-- a touched file whose documentation share fell as its comment "
            "or docstring lines disappeared; omitted, only the file, "
            "function, and complexity ratchets run"
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
