from __future__ import annotations

import stat
from pathlib import Path

import pytest

from atelier2.adapters.bwrap_sandbox import (
    SYSTEM_READ_ONLY_ROOTS,
    sandboxed_arguments,
    toolchain_sandbox,
    verified_sandbox_host,
)
from atelier2.contracts.sandbox_grants import (
    LEASED_DIRECTORY_DESCRIPTOR,
    SandboxedLaunch,
    SandboxGrants,
    SandboxUnavailable,
)

ENFORCER = Path("/usr/bin/bwrap")
WORKSPACE = Path("/workspaces/attempt-1")
TOOLCHAIN = Path("/toolchains/grok-1.0.5/grok")
PRIVATE_HOME = Path("/workspaces/grok-home")
COMMAND = (str(TOOLCHAIN), "-p", "read the file and answer")
END_OF_FLAGS = "--"


def _fenced(
    grants: SandboxGrants,
    descriptor: str | None = None,
    command: tuple[str, ...] = COMMAND,
) -> tuple[str, ...]:
    return sandboxed_arguments(
        command, WORKSPACE, SandboxedLaunch(ENFORCER, grants), descriptor
    )


def _bindings(arguments: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    """Every mount one fenced argv asks for, read back as flag, source, target."""

    fence = arguments[: arguments.index(END_OF_FLAGS)]
    return tuple(
        (argument, fence[index + 1], fence[index + 2])
        for index, argument in enumerate(fence)
        if argument.endswith(("bind", "bind-fd"))
    )


def _fake_enforcer(directory: Path, reported: str) -> Path:
    executable = directory / "bwrap"
    executable.write_text(f"#!/bin/sh\necho '{reported}'\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    return executable


@pytest.mark.parametrize(
    ("grants", "descriptor", "mounted"),
    (
        pytest.param(
            SandboxGrants(),
            None,
            ((("--bind", str(WORKSPACE), str(WORKSPACE))),),
            id="the directory a child stands in is the one directory it may write",
        ),
        pytest.param(
            SandboxGrants(readable_and_executable=(TOOLCHAIN,)),
            None,
            (
                ("--ro-bind", str(TOOLCHAIN), str(TOOLCHAIN)),
                ("--bind", str(WORKSPACE), str(WORKSPACE)),
            ),
            id="a granted toolchain is readable under the name it already has",
        ),
        pytest.param(
            SandboxGrants(writable=(PRIVATE_HOME,)),
            None,
            (
                ("--bind", str(PRIVATE_HOME), str(PRIVATE_HOME)),
                ("--bind", str(WORKSPACE), str(WORKSPACE)),
            ),
            id="a granted private home is writable beside the workspace",
        ),
        pytest.param(
            SandboxGrants(writable=(WORKSPACE,)),
            None,
            ((("--bind", str(WORKSPACE), str(WORKSPACE))),),
            id="a workspace that is already granted is not mounted twice",
        ),
        pytest.param(
            SandboxGrants(
                writable=(PRIVATE_HOME,), readable_and_executable=(TOOLCHAIN,)
            ),
            LEASED_DIRECTORY_DESCRIPTOR,
            (
                ("--ro-bind", str(TOOLCHAIN), str(TOOLCHAIN)),
                ("--bind", str(PRIVATE_HOME), str(PRIVATE_HOME)),
                ("--bind-fd", LEASED_DIRECTORY_DESCRIPTOR, str(WORKSPACE)),
            ),
            id="a leased workspace arrives as its descriptor and never as its path",
        ),
    ),
)
def test_a_grant_becomes_exactly_the_mounts_it_names(
    grants: SandboxGrants,
    descriptor: str | None,
    mounted: tuple[tuple[str, str, str], ...],
) -> None:
    assert _bindings(_fenced(grants, descriptor)) == mounted


def test_a_command_that_declared_no_grant_is_started_as_it_came() -> None:
    assert sandboxed_arguments(COMMAND, WORKSPACE, None) == COMMAND


def test_a_fenced_command_keeps_its_own_arguments_behind_the_end_of_flags() -> None:
    fenced = _fenced(SandboxGrants(readable_and_executable=(TOOLCHAIN,)))

    assert fenced[0] == str(ENFORCER)
    assert fenced[fenced.index(END_OF_FLAGS) + 1 :] == COMMAND


def test_a_fenced_command_unshares_every_namespace_but_the_network() -> None:
    """The file boundary is drawn here; a provider still reaches its own API."""

    fence = _fenced(SandboxGrants())[: _fenced(SandboxGrants()).index(END_OF_FLAGS)]

    assert "--unshare-all" in fence
    assert "--share-net" in fence
    assert "--die-with-parent" in fence
    assert fence[-2:] == ("--chdir", str(WORKSPACE))


def test_a_prompt_of_shell_metacharacters_stays_one_argument() -> None:
    """The fence prepends words to an argument vector; it never builds a line."""

    prompt = "'; touch /tmp/pwned; #"
    fenced = _fenced(SandboxGrants(), command=(str(TOOLCHAIN), "-p", prompt))

    assert fenced.count(prompt) == 1
    assert fenced[fenced.index(END_OF_FLAGS) + 1 :] == (str(TOOLCHAIN), "-p", prompt)


def test_a_grant_names_only_paths_that_exist_and_none_inside_another(
    tmp_path: Path,
) -> None:
    """The record is the whole reach, so it stays readable: no duplicate, no
    path already covered by a granted parent, and nothing bubblewrap would
    refuse the whole start over."""

    tools = tmp_path / "tools"
    tools.mkdir()
    _fake_enforcer(tools, "bubblewrap 0.9.0")
    toolchain = tools / "grok"
    toolchain.touch()
    absent = tmp_path / "gone"
    state = tmp_path / "state"
    state.mkdir()
    search_path = ":".join((str(tools), str(tmp_path), str(absent), "relative/bin"))

    grants = toolchain_sandbox(toolchain, search_path, state).grants

    assert grants.writable == (state,)
    assert set(grants.readable_and_executable) == {tmp_path} | {
        root for root in SYSTEM_READ_ONLY_ROOTS if root.exists()
    }


def test_a_deployment_without_bubblewrap_is_told_so_by_name(tmp_path: Path) -> None:
    with pytest.raises(SandboxUnavailable, match="search path"):
        verified_sandbox_host(str(tmp_path))


def test_a_bubblewrap_that_cannot_bind_a_descriptor_is_refused_by_its_version(
    tmp_path: Path,
) -> None:
    """Below 0.9.0 a leased directory could only be handed over as a path."""

    _fake_enforcer(tmp_path, "bubblewrap 0.8.0")

    with pytest.raises(SandboxUnavailable, match="0.8.0"):
        verified_sandbox_host(str(tmp_path))


def test_a_bubblewrap_that_reports_no_version_is_refused(tmp_path: Path) -> None:
    _fake_enforcer(tmp_path, "not a version")

    with pytest.raises(SandboxUnavailable, match="did not report a version"):
        verified_sandbox_host(str(tmp_path))
