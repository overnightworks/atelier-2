"""Which Grok releases this repository measured its invocation semantics against.

The version gate is its own module because it answers a question about the
host, not about a job: whether the executable a deployment names is the release
every measured flag, envelope and refusal in `grok_subscription` was read off.
Composition asks it once, the toolchain installer asks it after an install, and
neither needs to know how a job is prepared.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from atelier2.adapters.bounded_processes import bounded_process_answer

CONFORMANT_GROK_VERSIONS = frozenset({(1, 0, 5)})

GROK_PROBE_TIMEOUT_SECONDS = 30.0
"""How long any composition probe waits for this CLI to answer."""

_VERSION_FLAG = "--version"
_VERSION_PROBE_OUTPUT_BYTES = 4_096


class GrokExecutableUnsupported(ValueError):
    """The named executable is not a Grok CLI this executor was measured against."""


def _parsed_version(reported: str) -> tuple[int, int, int] | None:
    """Read `grok 1.0.5 (...)` as the version."""

    tokens = reported.strip().split()
    if not tokens:
        return None
    leading = (
        tokens[1] if tokens[0].lower() == "grok" and len(tokens) > 1 else tokens[0]
    )
    parts = leading.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def read_grok_version(
    executable: Path, timeout_seconds: float = GROK_PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Ask one executable which Grok it is. Runs it with `--version`."""

    with tempfile.TemporaryDirectory(prefix="atelier2-grok-version-") as probe_root:
        try:
            process = subprocess.Popen(
                (str(executable), _VERSION_FLAG),
                cwd=probe_root,
                env={},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise GrokExecutableUnsupported(
                f"the Grok executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
        try:
            return_code, answer = bounded_process_answer(
                process, timeout_seconds, _VERSION_PROBE_OUTPUT_BYTES
            )
        except OSError as error:
            raise GrokExecutableUnsupported(
                f"the Grok executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
    if return_code != 0:
        raise GrokExecutableUnsupported(
            f"the Grok executable refused {_VERSION_FLAG} with exit code {return_code}"
        )
    version = _parsed_version(answer.decode("utf-8", "replace"))
    if version is None:
        raise GrokExecutableUnsupported(
            f"the Grok executable did not report a version at {_VERSION_FLAG}"
        )
    return version


def verify_grok_capability(
    executable: Path, timeout_seconds: float = GROK_PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Refuse an executable outside the reviewed conformance set."""

    version = read_grok_version(executable, timeout_seconds)
    if version not in CONFORMANT_GROK_VERSIONS:
        reported = ".".join(str(part) for part in version)
        conformant = ", ".join(
            ".".join(str(part) for part in candidate)
            for candidate in sorted(CONFORMANT_GROK_VERSIONS)
        )
        raise GrokExecutableUnsupported(
            f"serving Grok subscription agents requires Grok {conformant}, "
            f"not {reported}: this executor's invocation semantics were measured "
            "against that exact release"
        )
    return version
