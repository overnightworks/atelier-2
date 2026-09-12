from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from atelier2.adapters.bwrap_sandbox import (
    SANDBOX_EXECUTABLE_NAME,
    SYSTEM_READ_ONLY_FILES,
    SYSTEM_READ_ONLY_ROOTS,
    resolved_sandbox_executable,
    sandbox_frame,
    sandbox_from_frame,
    sandboxed_arguments,
    toolchain_grants,
    verified_sandbox_host,
)
from atelier2.contracts.sandbox_grants import (
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
    descriptor: int = 7,
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


def _fake_enforcer(
    directory: Path, refuses: str = "", answers: str | None = None
) -> Path:
    """A stand-in enforcer: it refuses one option, or answers its own way."""

    executable = directory / "bwrap"
    told = 'cat "$@"' if answers is None else f"printf %s {answers!r}"
    executable.write_text(
        "#!/bin/sh\n"
        f'for argument in "$@"; do [ "$argument" = {refuses!r} ] && exit 2; done\n'
        'while [ "$1" != "--" ]; do shift; done\n'
        "shift 2\n"
        f"{told}\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    return executable


@pytest.mark.parametrize(
    ("grants", "mounted"),
    (
        pytest.param(
            SandboxGrants(),
            (("--bind-fd", "7", str(WORKSPACE)),),
            id="a child reaches the directory it stands in and nothing else",
        ),
        pytest.param(
            SandboxGrants(readable_and_executable=(TOOLCHAIN,)),
            (
                ("--ro-bind", str(TOOLCHAIN), str(TOOLCHAIN)),
                ("--bind-fd", "7", str(WORKSPACE)),
            ),
            id="a granted toolchain is readable under the name it already has",
        ),
        pytest.param(
            SandboxGrants(writable=(PRIVATE_HOME,)),
            (
                ("--bind", str(PRIVATE_HOME), str(PRIVATE_HOME)),
                ("--bind-fd", "7", str(WORKSPACE)),
            ),
            id="a granted private home is writable beside the workspace",
        ),
        pytest.param(
            SandboxGrants(writable=(WORKSPACE,)),
            (("--bind-fd", "7", str(WORKSPACE)),),
            id="a workspace named by the grant too is still bound by descriptor only",
        ),
        pytest.param(
            SandboxGrants(
                writable=(PRIVATE_HOME,), readable_and_executable=(TOOLCHAIN,)
            ),
            (
                ("--ro-bind", str(TOOLCHAIN), str(TOOLCHAIN)),
                ("--bind", str(PRIVATE_HOME), str(PRIVATE_HOME)),
                ("--bind-fd", "7", str(WORKSPACE)),
            ),
            id="every grant it names, and the workspace as its descriptor",
        ),
    ),
)
def test_a_grant_becomes_exactly_the_mounts_it_names(
    grants: SandboxGrants,
    mounted: tuple[tuple[str, str, str], ...],
) -> None:
    assert _bindings(_fenced(grants)) == mounted


def test_a_command_that_declared_no_grant_is_started_as_it_came() -> None:
    assert sandboxed_arguments(COMMAND, WORKSPACE, None, 7) == COMMAND


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


def _granted(tmp_path: Path) -> SandboxGrants:
    """What one toolchain standing in its own directory is granted."""

    tools = tmp_path / "tools"
    tools.mkdir()
    toolchain = tools / "grok"
    toolchain.touch()
    state = tmp_path / "state"
    state.mkdir()
    return toolchain_grants(toolchain, state)


def test_a_grant_names_the_toolchain_and_the_system_and_nothing_around_them(
    tmp_path: Path,
) -> None:
    """The directory a toolchain stands in is not the toolchain: granting it
    would hand over whatever else was installed, deployed or unpacked beside
    the executable, so the grant names the file and the system roots itself."""

    grants = _granted(tmp_path)

    assert grants.writable == (tmp_path / "state",)
    assert tmp_path not in grants.readable_and_executable
    assert tmp_path / "tools" not in grants.readable_and_executable
    assert set(grants.readable_and_executable) == {tmp_path / "tools" / "grok"} | {
        path
        for path in (*SYSTEM_READ_ONLY_ROOTS, *SYSTEM_READ_ONLY_FILES)
        if path.exists()
    }


def test_a_grant_hands_over_no_directory_of_this_account_beyond_its_own(
    tmp_path: Path,
) -> None:
    """The named `/etc` files, and not the directory that holds this host's
    accounts, services and credentials."""

    grants = _granted(tmp_path)

    assert Path("/etc") not in grants.readable_and_executable
    assert Path.home() not in grants.readable_and_executable


def test_a_grant_of_certificate_material_names_no_private_key_of_this_host(
    tmp_path: Path,
) -> None:
    """`/etc/ssl` holds both halves: the public store a client verifies with,
    and the keys any server on this account was issued. Only the first is what
    speaking HTTPS needs, so only the first is ever named."""

    granted = _granted(tmp_path).readable_and_executable

    assert Path("/etc/ssl") not in granted
    assert Path("/etc/ssl/private") not in granted
    assert not [path for path in granted if "private" in path.parts]


@pytest.mark.parametrize(
    "named",
    (
        pytest.param(
            lambda directory: resolved_sandbox_executable(str(directory)),
            id="a deployment whose search path carries no bubblewrap at all",
        ),
        pytest.param(
            lambda directory: _fake_enforcer(directory).relative_to(directory.anchor),
            id="a deployment that named one relatively",
        ),
    ),
)
def test_an_enforcer_this_deployment_cannot_name_absolutely_is_refused(
    tmp_path: Path, named: Callable[[Path], Path]
) -> None:
    """A name that is not an absolute path is resolved against whatever
    directory a launch stands in, which for this fence is a directory a
    provider writes -- so it is refused instead of started."""

    with pytest.raises(SandboxUnavailable, match=f"{SANDBOX_EXECUTABLE_NAME} at an"):
        verified_sandbox_host(named(tmp_path))


def test_a_bubblewrap_that_cannot_bind_a_descriptor_is_refused_by_that_option(
    tmp_path: Path,
) -> None:
    """The capability, not a version number: `--bind-fd` reached different
    releases through different distributions, and a build without it would
    leave the leased directory to be found by name."""

    with pytest.raises(SandboxUnavailable, match="did not hand a directory"):
        verified_sandbox_host(_fake_enforcer(tmp_path, refuses="--bind-fd"))


def test_a_bubblewrap_that_binds_nothing_is_refused_by_what_it_handed_over(
    tmp_path: Path,
) -> None:
    """A start that answers without the directory it was given proves nothing."""

    with pytest.raises(SandboxUnavailable, match="did not hand a directory"):
        verified_sandbox_host(_fake_enforcer(tmp_path, answers=""))


def test_an_enforcer_that_only_runs_what_stands_behind_the_flags_is_refused(
    tmp_path: Path,
) -> None:
    """The half a positive answer cannot carry.

    Every file the probe reads behind a true fence this account also reads
    without one, so a binary that merely executes the command behind `--`
    hands the marker back exactly as bubblewrap does. What tells them apart is
    the file outside every grant: the fence has no name for it, and an
    executable that answers with it fences nothing.
    """

    with pytest.raises(SandboxUnavailable, match="granted no name at all"):
        verified_sandbox_host(_fake_enforcer(tmp_path))


def test_a_grant_survives_the_launch_frame_it_travels_in() -> None:
    launch = SandboxedLaunch(
        ENFORCER, SandboxGrants((PRIVATE_HOME,), (TOOLCHAIN, Path("/usr")))
    )

    assert sandbox_from_frame(sandbox_frame(launch)) == launch
    assert sandbox_from_frame(sandbox_frame(None)) is None


@pytest.mark.parametrize(
    "frame",
    (
        pytest.param({"enforcer": str(ENFORCER)}, id="a grant with fields missing"),
        pytest.param(
            {"enforcer": 7, "readable_and_executable": [], "writable": []},
            id="an enforcer that is no path",
        ),
        pytest.param(
            {
                "enforcer": str(ENFORCER),
                "readable_and_executable": "/usr",
                "writable": [],
            },
            id="a grant that is no list",
        ),
        pytest.param(
            {
                "enforcer": str(ENFORCER),
                "readable_and_executable": ["../usr"],
                "writable": [],
            },
            id="a grant naming a path to be resolved",
        ),
    ),
)
def test_a_grant_this_reader_cannot_recognise_ends_the_launch(
    frame: dict[str, object],
) -> None:
    """The one place a boundary arrives from outside the process that draws it."""

    with pytest.raises(ValueError):
        sandbox_from_frame(frame)
