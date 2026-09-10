"""What a git remote call answers git with when git asks for the remote's credential.

The operator's token file is read and checked once per remote call, and git is
answered with exactly that checked value. Git never reads the operator's file
itself: a malformed file, or one swapped after the check, would otherwise reach
git's credential prompt, which prints any line it cannot parse.
"""

from __future__ import annotations

import os
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

    The token waits in an anonymous pipe of this process for the length of one
    git call: no file is created, and it is in no argument vector or
    environment variable. No child inherits the pipe; the helper opens it
    through this process's descriptor table, a path that names this pipe only
    while it is open. The pipe can be read once: a git process asking a second
    time gets an empty password, which ends as a failed login, not a hang or a
    leak. No token, no helper.
    """

    if token is None:
        yield ()
        return
    answer, sending = os.pipe()
    try:
        _deliver(sending, token.encode("ascii"))
        helper = (
            '!f() { test "$1" = get || exit 0; '
            "printf 'username=x-access-token\\npassword='; "
            f"/bin/cat /proc/{os.getpid()}/fd/{answer}; printf '\\n'; }}; f"
        )
        yield ("-c", f"credential.helper={helper}")
    finally:
        os.close(answer)


def _deliver(sending: int, encoded: bytes) -> None:
    """Put the whole token into the pipe and close its writing end, never blocking.

    Nothing reads the pipe yet, so a token larger than its buffer would block
    this process forever; it fails loud instead.
    """

    os.set_blocking(sending, False)
    try:
        delivered = os.write(sending, encoded)
    finally:
        os.close(sending)
    if delivered != len(encoded):
        raise OSError("the token does not fit into one pipe buffer")
