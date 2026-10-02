"""Install one already-conformant executor CLI into an atelier toolchain directory.

Versions are imported from the subscription adapters' CONFORMANT_* sets; this
script does not keep a second list. After the tree lands it asks the binary
``--version`` and refuses an answer that is not the selected member of that set.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path

from atelier2.adapters.claude_subscription import (
    CONFORMANT_CLAUDE_VERSIONS,
    ClaudeExecutableUnsupported,
    verify_claude_capability,
)
from atelier2.adapters.codex_subscription import (
    CONFORMANT_CODEX_VERSIONS,
    CodexExecutableUnsupported,
    verify_codex_capability,
)
from atelier2.adapters.grok_capability import (
    CONFORMANT_GROK_VERSIONS,
    verify_grok_capability,
)
from atelier2.adapters.grok_subscription import (
    GrokExecutableUnsupported,
)

Release = tuple[int, int, int]


class ExecutorKind(StrEnum):
    CLAUDE = "claude"
    GROK = "grok"
    CODEX = "codex"


class ToolchainInstallRefused(Exception):
    """The requested toolchain cannot be installed as a pinned executor."""


CONFORMANT_VERSIONS: Mapping[ExecutorKind, frozenset[Release]] = {
    ExecutorKind.CLAUDE: CONFORMANT_CLAUDE_VERSIONS,
    ExecutorKind.GROK: CONFORMANT_GROK_VERSIONS,
    ExecutorKind.CODEX: CONFORMANT_CODEX_VERSIONS,
}

# Proven operator installs: Claude and Codex land as npm prefixes; Grok is a
# copied standalone binary, not an npm package.
NPM_PACKAGES: Mapping[ExecutorKind, str] = {
    ExecutorKind.CLAUDE: "@anthropic-ai/claude-code",
    ExecutorKind.CODEX: "@openai/codex",
}

TOOLCHAIN_DIRECTORY_NAME = "atelier2-toolchains"
XDG_DATA_HOME_VARIABLE = "XDG_DATA_HOME"
HOME_VARIABLE = "HOME"
XDG_DATA_HOME_FALLBACK = Path(".local") / "share"
NPM_BIN_DIRECTORY = Path("node_modules") / ".bin"
SEARCH_PATH_VARIABLE = "PATH"
REFUSAL_PREFIX = "install executor toolchain"


def render_release(release: Release) -> str:
    return ".".join(str(part) for part in release)


def parsed_release(value: str) -> Release | None:
    parts = value.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def rendered_set(versions: frozenset[Release]) -> str:
    return ", ".join(render_release(release) for release in sorted(versions))


def default_toolchain_root() -> Path:
    xdg = os.environ.get(XDG_DATA_HOME_VARIABLE)
    if xdg:
        return Path(xdg) / TOOLCHAIN_DIRECTORY_NAME
    home = os.environ.get(HOME_VARIABLE)
    if home:
        return Path(home) / XDG_DATA_HOME_FALLBACK / TOOLCHAIN_DIRECTORY_NAME
    raise ToolchainInstallRefused("set HOME or XDG_DATA_HOME, or pass --root")


def toolchain_prefix(root: Path, kind: ExecutorKind, release: Release) -> Path:
    return root / f"{kind.value}-{render_release(release)}"


def toolchain_executable(kind: ExecutorKind, prefix: Path) -> Path:
    if kind is ExecutorKind.GROK:
        return prefix / kind.value
    return prefix / NPM_BIN_DIRECTORY / kind.value


def selected_release(kind: ExecutorKind, requested: str | None) -> Release:
    admitted = CONFORMANT_VERSIONS[kind]
    if requested is None:
        if len(admitted) == 1:
            return next(iter(admitted))
        raise ToolchainInstallRefused(
            f"{kind.value} has more than one conformant release "
            f"({rendered_set(admitted)}); name one with --version"
        )
    release = parsed_release(requested)
    if release is None:
        raise ToolchainInstallRefused(f"{requested!r} is not three dotted integers")
    if release not in admitted:
        raise ToolchainInstallRefused(
            f"{kind.value} {requested} is not a conformant release "
            f"({rendered_set(admitted)})"
        )
    return release


def npm_install_arguments(
    npm: Path, prefix: Path, kind: ExecutorKind, release: Release
) -> tuple[str, ...]:
    package = NPM_PACKAGES[kind]
    return (
        str(npm),
        "install",
        "--save-exact",
        "--prefix",
        str(prefix),
        f"{package}@{render_release(release)}",
    )


def run_npm(arguments: Sequence[str]) -> None:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise ToolchainInstallRefused("npm was not found") from error
    if completed.returncode == 0:
        return
    detail = completed.stderr.strip() or completed.stdout.strip()
    if detail:
        raise ToolchainInstallRefused(
            f"npm install refused with exit code {completed.returncode}: {detail}"
        )
    raise ToolchainInstallRefused(
        f"npm install refused with exit code {completed.returncode}"
    )


def plant_executable(kind: ExecutorKind, prefix: Path, source: Path) -> Path:
    if not source.is_file():
        raise ToolchainInstallRefused(f"--from must be a regular file, not {source}")
    destination = toolchain_executable(kind, prefix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        destination.chmod(0o755)
    except OSError as error:
        raise ToolchainInstallRefused(
            f"could not copy {source} into {destination}: {error}"
        ) from error
    return destination


def fetch_toolchain(
    kind: ExecutorKind,
    prefix: Path,
    release: Release,
    *,
    source: Path | None,
    npm: Path,
    run_command: Callable[[Sequence[str]], None],
) -> None:
    if source is not None:
        plant_executable(kind, prefix, source)
        return
    if kind not in NPM_PACKAGES:
        raise ToolchainInstallRefused(
            f"{kind.value} is a standalone binary; pass --from to the "
            "conformant executable"
        )
    prefix.mkdir(parents=True, exist_ok=True)
    run_command(npm_install_arguments(npm, prefix, kind, release))


def verify_installed(kind: ExecutorKind, executable: Path, search_path: str) -> Release:
    try:
        if kind is ExecutorKind.CLAUDE:
            return verify_claude_capability(executable)
        if kind is ExecutorKind.GROK:
            return verify_grok_capability(executable)
        return verify_codex_capability(executable, search_path)
    except (
        ClaudeExecutableUnsupported,
        GrokExecutableUnsupported,
        CodexExecutableUnsupported,
    ) as error:
        raise ToolchainInstallRefused(str(error)) from error


def install_executor_toolchain(
    kind: ExecutorKind,
    *,
    root: Path | None,
    requested_release: str | None,
    source: Path | None,
    npm: Path,
    run_command: Callable[[Sequence[str]], None] | None = None,
) -> Path:
    release = selected_release(kind, requested_release)
    prefix = toolchain_prefix(
        (root or default_toolchain_root()).expanduser().resolve(),
        kind,
        release,
    )
    fetch_toolchain(
        kind,
        prefix,
        release,
        source=source.expanduser().resolve() if source is not None else None,
        npm=npm,
        run_command=run_command or run_npm,
    )
    executable = toolchain_executable(kind, prefix)
    if not executable.is_file():
        raise ToolchainInstallRefused(
            f"{kind.value} did not land an executable at {executable}"
        )
    search_path = os.environ.get(SEARCH_PATH_VARIABLE)
    if search_path is None:
        raise ToolchainInstallRefused(
            "PATH is required so the installed executable can be probed"
        )
    reported = verify_installed(kind, executable, search_path)
    if reported != release:
        raise ToolchainInstallRefused(
            f"{kind.value} at {executable} reported {render_release(reported)}, "
            f"not {render_release(release)}"
        )
    return executable.resolve()


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_executor_toolchain",
        description=(
            "Install one already-conformant executor CLI into an atelier "
            "toolchain directory and print the absolute executable path. "
            "Versions are the CONFORMANT_* sets imported from the "
            "subscription adapters."
        ),
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=tuple(kind.value for kind in ExecutorKind),
        help="claude, grok, or codex",
    )
    parser.add_argument(
        "--version",
        help=(
            "a member of this provider's CONFORMANT_* set; required when that "
            "set has more than one release"
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        help=(
            "toolchain root (default: "
            "${XDG_DATA_HOME:-$HOME/.local/share}/atelier2-toolchains)"
        ),
    )
    parser.add_argument(
        "--from",
        dest="source_executable",
        type=Path,
        help="copy this executable into the toolchain directory instead of fetching",
    )
    parser.add_argument(
        "--npm",
        type=Path,
        default=Path("npm"),
        help="npm executable used to fetch Claude and Codex",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = argument_parser().parse_args(argv)
    try:
        executable = install_executor_toolchain(
            ExecutorKind(arguments.provider),
            root=arguments.root,
            requested_release=arguments.version,
            source=arguments.source_executable,
            npm=arguments.npm,
        )
    except ToolchainInstallRefused as error:
        print(f"{REFUSAL_PREFIX}: {error}", file=sys.stderr)
        return 1
    print(
        f"{REFUSAL_PREFIX}: {arguments.provider} is ready",
        file=sys.stderr,
    )
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
