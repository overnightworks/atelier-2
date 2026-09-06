"""No file this repository tracks may repeat the operator's own machine
identity: a home directory path or a hostname is a personal or hardware
identifier, not a credential, so no secret scanner catches it (audit
git-e2-session, 06.09.2026)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
PLACEHOLDER_USER = "operator"

_REAL_HOME_PATH = re.compile(rf"/home/(?!{PLACEHOLDER_USER}/)[^/\s\"'<>]+/")
_MACHINE_HOSTNAME = re.compile(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+-PC-[0-9]+\b")


def operator_identifier_matches(line: str) -> list[str]:
    """The forbidden identifier shapes this line contains, left to right."""

    return [
        match.group()
        for pattern in (_REAL_HOME_PATH, _MACHINE_HOSTNAME)
        for match in pattern.finditer(line)
    ]


def _tracked_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return [PROJECT_ROOT / relative for relative in listed]


def _findings() -> list[str]:
    findings: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary asset (image, …) cannot carry a text identifier
        for line_number, line in enumerate(text.splitlines(), start=1):
            findings.extend(
                f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {identifier}"
                for identifier in operator_identifier_matches(line)
            )
    return findings


def test_no_tracked_file_names_the_operators_real_home_path_or_hostname() -> None:
    assert _findings() == []


def test_the_matcher_flags_a_synthetic_home_path_and_hostname_but_not_the_placeholder() -> (
    None
):
    synthetic_home = "/home/" + "jane" + "/project"
    synthetic_hostname = "alpha-beta" + "-PC-" + "3"
    placeholder_home = "/home/" + PLACEHOLDER_USER + "/project"
    placeholder_hostname = PLACEHOLDER_USER + "-host"

    assert operator_identifier_matches(f"cwd={synthetic_home}") == ["/home/jane/"]
    assert operator_identifier_matches(f"host={synthetic_hostname}") == [
        synthetic_hostname
    ]
    assert operator_identifier_matches(f"cwd={placeholder_home}") == []
    assert operator_identifier_matches(f"host={placeholder_hostname}") == []
