"""What a git remote call answers git with when git asks for the remote's credential.

The operator's token file is read and checked once per remote call, and git is
answered with exactly that checked value. Git never reads the operator's file
itself: a malformed file, or one swapped after the check, would otherwise reach
git's credential prompt, which prints any line it cannot parse.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path


class TokenFileProblem(StrEnum):
    """Why a credential file holds no token a request may carry, never quoting it."""

    MISSING = "does not exist"
    UNREADABLE = "is not readable"
    EMPTY = "is empty"
    MALFORMED = "does not hold exactly one token of visible ASCII characters"


_VISIBLE_ASCII = range(0x21, 0x7F)
_PRIVATE_FILE_MODE = 0o600


def read_token_file(path: Path) -> str | TokenFileProblem:
    """The one token a credential file holds, or why it holds none.

    The owner of what a token file may hold, for every adapter that sends one:
    visible ASCII once the edges are trimmed, since anything else can reach an
    HTTP header or a git prompt where a protocol error or a log prints it.
    """

    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return TokenFileProblem.MISSING
    except OSError:
        return TokenFileProblem.UNREADABLE
    token = contents.strip()
    if not token:
        return TokenFileProblem.EMPTY
    if any(byte not in _VISIBLE_ASCII for byte in token):
        return TokenFileProblem.MALFORMED
    return token.decode("ascii")


@contextmanager
def credential_helper_arguments(token: str | None) -> Iterator[tuple[str, ...]]:
    """`git -c` arguments whose credential helper answers git with exactly `token`.

    The token is in no argument vector and no environment variable: it waits in
    a file only this user can read, inside a private directory that is removed
    when the git call returns, however it returns. No token, no helper.
    """

    if token is None:
        yield ()
        return
    with tempfile.TemporaryDirectory(prefix="atelier2-git-credential-") as private:
        answer = Path(private) / "token"
        descriptor = os.open(
            answer, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as written:
            written.write(token)
        helper = (
            '!f() { test "$1" = get || exit 0; '
            "printf 'username=x-access-token\\npassword='; "
            f"/bin/cat {shlex.quote(str(answer))}; printf '\\n'; }}; f"
        )
        yield ("-c", f"credential.helper={helper}")
