"""Files the operator names on the command line are read from where the command runs."""

from __future__ import annotations

from pathlib import Path


class OperatorFileRefused(ValueError):
    """The command will not read the file the operator named."""


def read_operator_file(named: Path, working_directory: Path) -> bytes:
    """Read one named file from beneath the working directory, or refuse it.

    A relative name is taken from the working directory, and an absolute one
    must lie beneath it once every link is followed: a command that read
    whatever path its arguments spell would hand a mistyped or planted argument
    any file the operator can open.
    """

    root = working_directory.resolve()
    resolved = (working_directory / named).resolve()
    if not resolved.is_relative_to(root):
        raise OperatorFileRefused(
            f"{named} lies outside the working directory {root}, so this command "
            "will not read it"
        )
    try:
        return resolved.read_bytes()
    except OSError as unreadable:
        raise OperatorFileRefused(
            f"cannot read {named}: {unreadable.strerror}"
        ) from unreadable
