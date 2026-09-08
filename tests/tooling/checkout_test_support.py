"""Whether a directory is a git checkout, for tests that inspect versioned files."""

from __future__ import annotations

from pathlib import Path

MISSING_CHECKOUT_REASON = (
    "this tree is not a git checkout, so there are no versioned files to inspect"
)


def directory_is_a_git_checkout(directory: Path) -> bool:
    return (directory / ".git").exists()
