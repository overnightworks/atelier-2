from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from atelier2.adapters.claude_subscription import CONFORMANT_CLAUDE_VERSIONS
from atelier2.adapters.codex_subscription import CONFORMANT_CODEX_VERSIONS
from atelier2.adapters.grok_subscription import CONFORMANT_GROK_VERSIONS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "install_executor_toolchain.py"
OPERATIONS = PROJECT_ROOT / "docs" / "OPERATIONS.md"
DOCUMENTATION_MAP = PROJECT_ROOT / "docs" / "README.md"
Release = tuple[int, int, int]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "install_executor_toolchain", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


toolchain = load_script()


def render(release: Release) -> str:
    return ".".join(str(part) for part in release)


def a_release_outside(versions: frozenset[Release]) -> Release:
    major, minor, patch = max(versions)
    candidate = (major, minor, patch + 1)
    while candidate in versions:
        patch += 1
        candidate = (major, minor, patch)
    return candidate


def version_line(provider: str, release: Release) -> str:
    rendered = render(release)
    if provider == "claude":
        return f"{rendered} (Claude Code)"
    if provider == "grok":
        return f"grok {rendered} (test) [stable]"
    return f"codex-cli {rendered}"


def relative_executable(provider: str, release: Release) -> Path:
    prefix = Path(f"{provider}-{render(release)}")
    if provider == "grok":
        return prefix / "grok"
    return prefix / "node_modules" / ".bin" / provider


def write_fake_executable(path: Path, reported: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            f"#!{sys.executable}\n"
            "import sys\n"
            'if "--version" in sys.argv:\n'
            f"    print({reported!r})\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n"
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("provider", "versions"),
    (
        ("claude", CONFORMANT_CLAUDE_VERSIONS),
        ("grok", CONFORMANT_GROK_VERSIONS),
        ("codex", CONFORMANT_CODEX_VERSIONS),
    ),
)
def test_a_version_outside_the_conformant_set_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    versions: frozenset[Release],
) -> None:
    outside = render(a_release_outside(versions))
    source = write_fake_executable(
        tmp_path / "source", version_line(provider, min(versions))
    )

    status = toolchain.main(
        [
            "--provider",
            provider,
            "--version",
            outside,
            "--root",
            str(tmp_path / "toolchains"),
            "--from",
            str(source),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "not a conformant release" in captured.err
    assert outside in captured.err
    for release in versions:
        assert render(release) in captured.err
    assert captured.out == ""
    toolchains = tmp_path / "toolchains"
    assert not toolchains.exists() or not any(toolchains.iterdir())


@pytest.mark.parametrize(
    ("provider", "versions"),
    (
        ("claude", CONFORMANT_CLAUDE_VERSIONS),
        ("grok", CONFORMANT_GROK_VERSIONS),
        ("codex", CONFORMANT_CODEX_VERSIONS),
    ),
)
def test_an_installed_binary_outside_the_conformant_set_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    versions: frozenset[Release],
) -> None:
    selected = min(versions)
    source = write_fake_executable(
        tmp_path / "source",
        version_line(provider, a_release_outside(versions)),
    )

    status = toolchain.main(
        [
            "--provider",
            provider,
            "--version",
            render(selected),
            "--root",
            str(tmp_path / "toolchains"),
            "--from",
            str(source),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "install executor toolchain:" in captured.err
    planted = tmp_path / "toolchains" / relative_executable(provider, selected)
    assert planted.is_file()


@pytest.mark.parametrize(
    ("provider", "versions"),
    (
        ("claude", CONFORMANT_CLAUDE_VERSIONS),
        ("grok", CONFORMANT_GROK_VERSIONS),
        ("codex", CONFORMANT_CODEX_VERSIONS),
    ),
)
def test_a_conformant_source_lands_in_the_toolchain_layout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    versions: frozenset[Release],
) -> None:
    selected = min(versions)
    source = write_fake_executable(
        tmp_path / "source", version_line(provider, selected)
    )
    root = tmp_path / "toolchains"
    expected = (root / relative_executable(provider, selected)).resolve()

    status = toolchain.main(
        [
            "--provider",
            provider,
            "--version",
            render(selected),
            "--root",
            str(root),
            "--from",
            str(source),
        ]
    )

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out.strip() == str(expected)
    assert expected.is_file()
    assert os.access(expected, os.X_OK)


def test_the_script_uses_the_adapter_conformant_sets() -> None:
    assert (
        toolchain.CONFORMANT_VERSIONS[toolchain.ExecutorKind.CLAUDE]
        is CONFORMANT_CLAUDE_VERSIONS
    )
    assert (
        toolchain.CONFORMANT_VERSIONS[toolchain.ExecutorKind.GROK]
        is CONFORMANT_GROK_VERSIONS
    )
    assert (
        toolchain.CONFORMANT_VERSIONS[toolchain.ExecutorKind.CODEX]
        is CONFORMANT_CODEX_VERSIONS
    )


def test_claude_requires_an_explicit_version_when_several_releases_are_conformant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert len(CONFORMANT_CLAUDE_VERSIONS) > 1
    source = write_fake_executable(
        tmp_path / "source",
        version_line("claude", min(CONFORMANT_CLAUDE_VERSIONS)),
    )

    status = toolchain.main(
        [
            "--provider",
            "claude",
            "--root",
            str(tmp_path / "toolchains"),
            "--from",
            str(source),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "--version" in captured.err
    for release in CONFORMANT_CLAUDE_VERSIONS:
        assert render(release) in captured.err
    assert not (tmp_path / "toolchains").exists() or not any(
        (tmp_path / "toolchains").iterdir()
    )


def test_grok_refuses_to_fetch_without_a_source_binary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = toolchain.main(
        ["--provider", "grok", "--root", str(tmp_path / "toolchains")]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "--from" in captured.err
    assert "standalone binary" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "toolchains").exists() or not any(
        (tmp_path / "toolchains").iterdir()
    )


def test_an_installed_binary_that_is_conformant_but_not_the_selected_release_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ordered = sorted(CONFORMANT_CLAUDE_VERSIONS)
    assert len(ordered) >= 2
    selected, other = ordered[0], ordered[1]
    source = write_fake_executable(tmp_path / "source", version_line("claude", other))

    status = toolchain.main(
        [
            "--provider",
            "claude",
            "--version",
            render(selected),
            "--root",
            str(tmp_path / "toolchains"),
            "--from",
            str(source),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "reported" in captured.err
    assert render(other) in captured.err
    assert render(selected) in captured.err


def test_a_fetch_that_lands_no_executable_is_refused(tmp_path: Path) -> None:
    release = min(CONFORMANT_CODEX_VERSIONS)

    with pytest.raises(toolchain.ToolchainInstallRefused, match="did not land"):
        toolchain.install_executor_toolchain(
            toolchain.ExecutorKind.CODEX,
            root=tmp_path / "toolchains",
            requested_release=render(release),
            source=None,
            npm=Path("npm"),
            run_command=lambda _arguments: None,
        )


def test_claude_and_codex_fetch_the_exact_conformant_npm_package(
    tmp_path: Path,
) -> None:
    recorded: list[tuple[str, ...]] = []

    def fake_npm(arguments: Sequence[str]) -> None:
        recorded.append(tuple(arguments))
        prefix = Path(arguments[arguments.index("--prefix") + 1])
        spec = next(item for item in arguments if item.startswith("@"))
        kind = (
            toolchain.ExecutorKind.CLAUDE
            if "claude-code" in spec
            else toolchain.ExecutorKind.CODEX
        )
        release = min(toolchain.CONFORMANT_VERSIONS[kind])
        write_fake_executable(
            toolchain.toolchain_executable(kind, prefix),
            version_line(kind.value, release),
        )

    for kind, package in (
        (toolchain.ExecutorKind.CLAUDE, "@anthropic-ai/claude-code"),
        (toolchain.ExecutorKind.CODEX, "@openai/codex"),
    ):
        recorded.clear()
        release = min(toolchain.CONFORMANT_VERSIONS[kind])
        root = tmp_path / kind.value
        installed = toolchain.install_executor_toolchain(
            kind,
            root=root,
            requested_release=render(release),
            source=None,
            npm=Path("npm"),
            run_command=fake_npm,
        )
        assert recorded
        assert "--save-exact" in recorded[0]
        assert f"{package}@{render(release)}" in recorded[0]
        assert installed == (root / relative_executable(kind.value, release)).resolve()


def test_the_default_root_follows_xdg_data_home(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    release = min(CONFORMANT_CODEX_VERSIONS)
    source = write_fake_executable(tmp_path / "source", version_line("codex", release))
    expected = (
        data_home / "atelier2-toolchains" / relative_executable("codex", release)
    ).resolve()

    status = toolchain.main(["--provider", "codex", "--from", str(source)])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out.strip() == str(expected)
    assert expected.is_file()


def operations_claude_install_command(runbook: str) -> str:
    for block in re.findall(r"```bash\n(.*?)```", runbook, flags=re.DOTALL):
        for line in block.splitlines():
            if "install_executor_toolchain.py" in line and "--provider claude" in line:
                return line
    raise AssertionError("OPERATIONS.md has no Claude toolchain install command")


def test_operations_describes_the_pinned_toolchain_not_the_daily_cli() -> None:
    mapping = DOCUMENTATION_MAP.read_text(encoding="utf-8")
    runbook = OPERATIONS.read_text(encoding="utf-8")

    assert "install_executor_toolchain" in runbook
    assert "atelier2-toolchains" in runbook
    assert "~/.local/bin/grok" in runbook
    assert "--claude-executable" in runbook
    assert "--grok-executable" in runbook
    assert "--codex-executable" in runbook
    assert "CONFORMANT_" in runbook
    assert "How is an executor toolchain pinned?" in mapping


def test_the_operations_claude_fence_names_a_version_when_several_releases_are_conformant() -> (
    None
):
    """A Claude fence without --version is exit 1; the runbook must not teach that."""

    assert len(CONFORMANT_CLAUDE_VERSIONS) > 1
    command = operations_claude_install_command(OPERATIONS.read_text(encoding="utf-8"))
    tokens = command.split()
    assert "--version" in tokens
    stated = tokens[tokens.index("--version") + 1]
    parts = stated.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
    assert (int(parts[0]), int(parts[1]), int(parts[2])) in CONFORMANT_CLAUDE_VERSIONS
