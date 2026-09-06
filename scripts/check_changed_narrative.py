"""Reject newly added provenance narrative in Python comments and docstrings."""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterator
from pathlib import Path

from report_corridor import CorridorError, git_diff_lines

CHECKED_SOURCE_ROOTS = ("src", "scripts")
NARRATIVE_PATTERN = re.compile(
    r"\b(?:formerly|superseded|since\s+PR\b|PR\s*#|"
    r"\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}|"
    r"(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december|januar|februar|märz|maerz|april|mai|juni|juli|august|"
    r"september|oktober|november|dezember)\s+\d{4})"
    r"|\breplaced\b(?:\s+\S+){1,4}\s+with\b"
    r"|(?<!\w)#\d+\b",
    re.IGNORECASE,
)
DECISION_OWNER_REFERENCE = re.compile(
    r"(?:ADR\s+\d+|docs/(?:decisions|requirements)/\S+|"
    r"REQ-[A-Za-z0-9][A-Za-z0-9._-]*)"
)
HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class NarrativeGateError(Exception):
    pass


def _decode_git_path(path: str) -> str:
    if not path.endswith('"'):
        raise NarrativeGateError(f"could not read patch path {path!r}")
    escaped_characters = {
        "a": b"\a",
        "b": b"\b",
        "f": b"\f",
        "n": b"\n",
        "r": b"\r",
        "t": b"\t",
        "v": b"\v",
        "\\": b"\\",
        '"': b'"',
    }
    encoded_path = bytearray()
    quoted_path = path[1:-1]
    index = 0
    while index < len(quoted_path):
        character = quoted_path[index]
        if character != "\\":
            encoded_path.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index == len(quoted_path):
            raise NarrativeGateError(f"could not read patch path {path!r}")
        escaped = quoted_path[index]
        if escaped in escaped_characters:
            encoded_path.extend(escaped_characters[escaped])
            index += 1
            continue
        if escaped not in "01234567":
            raise NarrativeGateError(f"could not read patch path {path!r}")
        octal_end = min(index + 3, len(quoted_path))
        octal = quoted_path[index:octal_end]
        if not octal.isascii() or any(digit not in "01234567" for digit in octal):
            raise NarrativeGateError(f"could not read patch path {path!r}")
        encoded_path.append(int(octal, 8))
        index = octal_end
    try:
        return bytes(encoded_path).decode("utf-8")
    except UnicodeDecodeError as error:
        raise NarrativeGateError(f"could not decode patch path {path!r}") from error


def _patch_path(header: str) -> Path | None:
    path = header.removeprefix("+++ ")
    if path == "/dev/null":
        return None
    if path.startswith('"'):
        path = _decode_git_path(path)
    if not path.startswith("b/"):
        raise NarrativeGateError(f"could not read patch path {path!r}")
    return Path(path.removeprefix("b/"))


def _checked_source_path(header: str) -> Path | None:
    """The Python path a `+++` header names under a checked root, else None."""
    path = _patch_path(header)
    if (
        path is None
        or path.suffix != ".py"
        or path.parts[0] not in CHECKED_SOURCE_ROOTS
    ):
        return None
    return path


def _added_line_numbers(
    project_root: Path, base: str, head: str
) -> dict[Path, set[int]]:
    """Added lines per checked file. The diff runs over the whole tree on purpose:
    a pathspec would hide the source of a file moved in from outside it, and git
    would then report the move as a full add."""
    changed_lines: dict[Path, set[int]] = {}
    current_path: Path | None = None
    next_line_number: int | None = None
    for diff_line in git_diff_lines(
        project_root, base, head, "-U0", rename_detection="-M"
    ):
        if diff_line.startswith("diff --git "):
            current_path = None
            next_line_number = None
            continue
        if diff_line.startswith("+++ "):
            current_path = _checked_source_path(diff_line)
            continue
        hunk = HUNK_PATTERN.match(diff_line)
        if hunk is not None:
            next_line_number = int(hunk.group(1))
            continue
        if current_path is None or next_line_number is None:
            continue
        if diff_line.startswith("+") and not diff_line.startswith("+++"):
            changed_lines.setdefault(current_path, set()).add(next_line_number)
            next_line_number += 1
        elif diff_line.startswith(" "):
            next_line_number += 1
    return changed_lines


def _comment_texts(source: str) -> dict[int, str]:
    return {
        token.start[0]: token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }


def _docstring_texts(source_lines: list[str], tree: ast.AST) -> dict[int, str]:
    """Per docstring line, only the docstring's own slice of that physical
    line -- never any code sharing the line, such as a one-line function's
    header before the opening quotes."""

    texts: dict[int, str] = {}
    nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, nodes) or not node.body:
            continue
        statement = node.body[0]
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
            and statement.end_lineno is not None
            and statement.end_col_offset is not None
        ):
            continue
        for line_number in range(statement.lineno, statement.end_lineno + 1):
            line = source_lines[line_number - 1]
            start_column = (
                statement.col_offset if line_number == statement.lineno else 0
            )
            end_column = (
                statement.end_col_offset
                if line_number == statement.end_lineno
                else len(line)
            )
            texts[line_number] = line[start_column:end_column]
    return texts


def _narrative_texts(source: str) -> dict[int, str]:
    """A line can carry both a docstring slice and a trailing comment (a
    one-line docstring followed by `# ...`); search both, never let one
    overwrite the other."""

    tree = ast.parse(source)
    texts = dict(_docstring_texts(source.splitlines(), tree))
    for line_number, comment_text in _comment_texts(source).items():
        docstring_text = texts.get(line_number)
        texts[line_number] = (
            comment_text
            if docstring_text is None
            else f"{docstring_text}\n{comment_text}"
        )
    return texts


def _reference_only(line: str) -> bool:
    value = line.strip()
    if value.startswith("#"):
        value = value[1:].strip()
    for delimiter in ('"""', "'''"):
        if value.startswith(delimiter):
            value = value.removeprefix(delimiter).strip()
        if value.endswith(delimiter):
            value = value.removesuffix(delimiter).strip()
    return DECISION_OWNER_REFERENCE.fullmatch(value) is not None


def _source_at_revision(project_root: Path, head: str, relative_path: Path) -> str:
    result = subprocess.run(
        ["git", "show", f"{head}:{relative_path.as_posix()}"],
        cwd=project_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise NarrativeGateError(
            f"could not read {relative_path} at {head}: {result.stderr.decode().strip()}"
        )
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(result.stdout).readline)
        return result.stdout.decode(encoding)
    except (SyntaxError, UnicodeDecodeError) as error:
        raise NarrativeGateError(
            f"could not decode {relative_path} at {head}: {error}"
        ) from error


def _findings(
    project_root: Path, base: str, head: str
) -> Iterator[tuple[Path, int, str]]:
    for relative_path, added_lines in _added_line_numbers(
        project_root, base, head
    ).items():
        try:
            contents = _source_at_revision(project_root, head, relative_path)
            narrative_texts = _narrative_texts(contents)
        except (NarrativeGateError, SyntaxError, tokenize.TokenError) as error:
            raise NarrativeGateError(
                f"could not inspect {relative_path}: {error}"
            ) from error
        source_lines = contents.splitlines()
        for line_number in sorted(added_lines & narrative_texts.keys()):
            narrative_text = narrative_texts[line_number]
            if NARRATIVE_PATTERN.search(narrative_text) and not _reference_only(
                narrative_text
            ):
                yield relative_path, line_number, source_lines[line_number - 1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="the range's base revision")
    parser.add_argument(
        "--head", default="HEAD", help="the range's head revision (default: HEAD)"
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        findings = tuple(_findings(Path.cwd(), arguments.base, arguments.head))
    except (CorridorError, NarrativeGateError) as error:
        print(f"changed narrative check failed: {error}", file=sys.stderr)
        return 1
    if not findings:
        return 0
    for path, line_number, line in findings:
        print(f"{path}:{line_number}: {line}")
    print(
        "AGENTS.md forbids added comments and docstrings from carrying ageing narrative."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
