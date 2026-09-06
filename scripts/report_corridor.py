"""Reports whether a change stays inside AGENTS.md's slice corridor.

AGENTS.md: "Keep a slice inside three production files and a hundred changed
production lines -- additions and deletions counted apart, tests and generated
files excluded. Above that corridor the dispatch names in one sentence why the
change does not split; the corridor is reported, never gated, because a check
cannot judge a cut." This script is that report: it always exits 0, prints one
summary line, and -- only once the corridor is exceeded -- writes that line to
the job summary and to a single, updated-in-place pull request comment.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

FILE_LIMIT = 3
LINE_LIMIT = 100
CORRIDOR_COMMENT_MARKER = "<!-- corridor-report -->"


class ProductionScope(NamedTuple):
    """AGENTS.md's corridor rule: production is a tracked file under one of
    these directories, unless it is named here as a generated exception.
    `tests/api/openapi_frozen.json` and the lockfiles already fall outside
    these directories, so they need no entry. `frontend/src/api/client.ts` is
    hand-written, not generated, so it stays counted. This tuple is where a
    future generated file under `src/` or `frontend/src/` would be named.
    """

    included_directory_prefixes: tuple[str, ...]
    excluded_generated_paths: tuple[str, ...]


PRODUCTION_SCOPE = ProductionScope(
    included_directory_prefixes=("src/", "frontend/src/"),
    excluded_generated_paths=(),
)


class CorridorError(Exception):
    pass


class CorridorReport(NamedTuple):
    file_count: int
    added_lines: int
    deleted_lines: int

    @property
    def changed_lines(self) -> int:
        return self.added_lines + self.deleted_lines

    @property
    def over_corridor(self) -> bool:
        return self.file_count > FILE_LIMIT or self.changed_lines > LINE_LIMIT

    def summary_line(self) -> str:
        return (
            f"corridor: {self.file_count} production files, "
            f"+{self.added_lines}/-{self.deleted_lines} lines "
            f"(limit {FILE_LIMIT} files, {LINE_LIMIT} lines)"
        )


def _is_production_path(path: str, scope: ProductionScope) -> bool:
    if path in scope.excluded_generated_paths:
        return False
    return path.startswith(scope.included_directory_prefixes)


def _parse_numstat_line(line: str) -> tuple[int, int, str]:
    """One `git diff --numstat` line. A binary file reports `-` for both counts;
    it still counts as a changed file, just with no line counts to add."""

    added_field, deleted_field, path = line.split("\t", 2)
    added = 0 if added_field == "-" else int(added_field)
    deleted = 0 if deleted_field == "-" else int(deleted_field)
    return added, deleted, path


def git_diff_lines(
    project_root: Path,
    base: str,
    head: str,
    *arguments: str,
    rename_detection: str,
    pathspecs: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """`rename_detection` is `--no-renames` or a `-M` flag; it has no default
    because a numstat file count and an added-lines diff need opposite answers
    to "does a rename count as a full add"."""

    command = ["git", "diff", rename_detection, *arguments, f"{base}...{head}"]
    if pathspecs:
        command.extend(("--", *pathspecs))
    result = subprocess.run(
        command,
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CorridorError(f"git diff failed: {result.stderr.strip()}")
    return tuple(result.stdout.splitlines())


def corridor_report(project_root: Path, base: str, head: str) -> CorridorReport:
    file_count = added_total = deleted_total = 0
    for line in git_diff_lines(
        project_root, base, head, "--numstat", rename_detection="--no-renames"
    ):
        if not line.strip():
            continue
        added, deleted, path = _parse_numstat_line(line)
        if not _is_production_path(path, PRODUCTION_SCOPE):
            continue
        file_count += 1
        added_total += added
        deleted_total += deleted
    return CorridorReport(file_count, added_total, deleted_total)


def _write_step_summary(line: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _existing_comment_id(repository: str, pull_request_number: int) -> int | None:
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repository}/issues/{pull_request_number}/comments",
            "--paginate",
            "--jq",
            f'.[] | select(.body | startswith("{CORRIDOR_COMMENT_MARKER}")) | .id',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    ids = [int(line) for line in result.stdout.splitlines() if line.strip()]
    if len(ids) > 1:
        raise CorridorError(
            "more than one corridor-report comment already exists; refusing to "
            "guess which one to update"
        )
    return ids[0] if ids else None


def _upsert_pull_request_comment(
    repository: str, pull_request_number: int, summary: str
) -> None:
    body = f"{CORRIDOR_COMMENT_MARKER}\n{summary}\n"
    comment_id = _existing_comment_id(repository, pull_request_number)
    if comment_id is None:
        endpoint = f"repos/{repository}/issues/{pull_request_number}/comments"
        method = "POST"
    else:
        endpoint = f"repos/{repository}/issues/comments/{comment_id}"
        method = "PATCH"
    subprocess.run(
        ["gh", "api", endpoint, "-X", method, "-f", f"body={body}"],
        check=True,
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="the range's base revision")
    parser.add_argument(
        "--head", default="HEAD", help="the range's head revision (default: HEAD)"
    )
    parser.add_argument(
        "--pull-request-number",
        type=int,
        default=None,
        help="post/update the report as a comment on this pull request",
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/repo for the pull request comment (default: $GITHUB_REPOSITORY)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    project_root = Path.cwd()
    try:
        report = corridor_report(project_root, arguments.base, arguments.head)
    except CorridorError as error:
        print(f"corridor report refused: {error}", file=sys.stderr)
        return 0
    summary = report.summary_line()
    print(summary, flush=True)
    if not report.over_corridor:
        return 0
    _write_step_summary(summary)
    if arguments.pull_request_number is not None:
        if not arguments.repository:
            print(
                "corridor report: no repository known "
                "(pass --repository or set $GITHUB_REPOSITORY); "
                "not posting a pull request comment",
                file=sys.stderr,
            )
            return 0
        try:
            _upsert_pull_request_comment(
                arguments.repository, arguments.pull_request_number, summary
            )
        except (CorridorError, subprocess.CalledProcessError) as error:
            print(
                f"corridor report: could not post the pull request comment: {error}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
