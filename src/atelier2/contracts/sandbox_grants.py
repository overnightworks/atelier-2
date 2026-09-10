"""What one provider child may reach on this host, stated once and completely.

A child that opens doors -- a shell, a file writer -- runs on the same account
as the server that started it. This grant is the whole answer to what it may
touch: what a start does not name here is either absent from the child's
filesystem or read-only to it. ADR 0009 §1 owns the containment doctrine the
record serves; ADR 0011 names the vectors it is drawn around.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LEASED_DIRECTORY_BIND = "--bind-fd"
LEASED_DIRECTORY_DESCRIPTOR = "{atelier2-leased-directory-descriptor}"
"""How a fenced argv says the leased working directory will be handed over.

The directory travels as the descriptor its launcher opened and checked, and a
descriptor number exists only inside the process that opens it -- which is not
the process that composes the argv. So the argv carries this word behind the
flag above, and the launching process replaces it with the number it opened.

The two are one token, never one alone: a launcher that replaced the word
wherever it stood would rewrite a job's own argument that happened to spell
it, and a flag without the word would name a descriptor nobody opened.
"""


class SandboxUnavailable(ValueError):
    """This host cannot enforce a grant, so nothing may be started behind it.

    Refusing is the point: a vector is armed because its child is fenced, so a
    start that cannot be fenced is not the same start with one property less.
    """


@dataclass(frozen=True)
class SandboxGrants:
    """Everything one child may write, and everything it may read and run.

    Two fields, and deliberately no third. A denial list beside an allowance
    list states one boundary twice and the copy is what goes stale. The
    environment needs no field, because the port already hands a child its
    complete environment rather than an overlay. A domain list would be a field
    with no enforcer behind it.

    The working directory is no field either. It reaches the enforcer as the
    open descriptor its launcher verified, so nothing here names a path that
    another directory could stand in for between the check and the start.
    """

    writable: tuple[Path, ...] = ()
    readable_and_executable: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        for path in (*self.writable, *self.readable_and_executable):
            if not path.is_absolute():
                raise ValueError(f"a sandbox grant names absolute paths only: {path}")


@dataclass(frozen=True)
class SandboxedLaunch:
    """One grant and the executable this host enforces it with."""

    enforcer: Path
    grants: SandboxGrants

    def __post_init__(self) -> None:
        if not self.enforcer.is_absolute():
            raise ValueError("a sandbox enforcer is named by its absolute path")
