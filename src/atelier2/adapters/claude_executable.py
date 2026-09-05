"""The Claude Code binary path, owned independently of any deployment.

`ClaudeSubscriptionSettings` (`claude_subscription.py`) layers credential
directory, search path, and bubblewrap requirements on top of this value for
the headless executor; a caller that only launches the binary interactively
needs exactly this invariant and nothing more.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClaudeExecutable:
    """A path validated as an existing executable file."""

    path: Path

    def __post_init__(self) -> None:
        resolved = self.path.resolve()
        object.__setattr__(self, "path", resolved)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError("the Claude executable must be an executable file")
