from __future__ import annotations

import hashlib
import os
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.contracts.host_configuration import ProjectId
from atelier2.host.terminal_seat import (
    SeatCommandResult,
    SeatMcpDocument,
    TerminalSeat,
    TerminalSeatCommandFailed,
    TerminalSeatOutcome,
    TerminalSeatSettings,
)

PROJECT_ID = ProjectId("atelier zwei")
DATABASE_PATH = "/var/lib/atelier2/atelier.db"
MCP_DOCUMENT = SeatMcpDocument(
    "seat-mcp.json",
    '{"mcpServers":{"atelier2":{"command":"/usr/bin/python3",'
    '"args":["-m","atelier2","mcp","--service","http://127.0.0.1:8422"]}}}',
)
SEAT_PORT = 7681
TMUX_WITHOUT_SERVER = "no server running on /tmp/tmux-1000/atelier-seat"
TMUX_UNREADABLE = "error connecting to /tmp/tmux-1000/atelier-seat (Permission denied)"
SYSTEMCTL_INACTIVE_EXIT_CODE = 3
SYSTEMCTL_BUS_UNREACHABLE = SeatCommandResult(1, "Failed to connect to bus")


def digest_of(database_path: str, project_id: str) -> str:
    return hashlib.sha256(
        b"\0".join((database_path.encode("utf-8"), project_id.encode("utf-8")))
    ).hexdigest()


@dataclass
class FakeSeatHost:
    """A machine that only remembers what was asked of it."""

    programs: dict[str, Path] = field(
        default_factory=lambda: {
            "systemd-run": Path("/usr/bin/systemd-run"),
            "systemctl": Path("/usr/bin/systemctl"),
        }
    )
    session_alive: bool = False
    scope_active: bool = False
    free_port: bool = True
    tmux_probe_unreadable: bool = False
    scope_probe_unreadable: bool = False
    exit_codes: dict[str, int] = field(default_factory=dict)
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> SeatCommandResult:
        command = tuple(argv)
        self.commands.append(command)
        if "has-session" in command:
            if self.tmux_probe_unreadable:
                return SeatCommandResult(1, TMUX_UNREADABLE)
            if self.session_alive:
                return SeatCommandResult(0)
            return SeatCommandResult(1, TMUX_WITHOUT_SERVER)
        if "is-active" in command:
            if self.scope_probe_unreadable:
                return SYSTEMCTL_BUS_UNREACHABLE
            if self.scope_active:
                return SeatCommandResult(0)
            return SeatCommandResult(SYSTEMCTL_INACTIVE_EXIT_CODE)
        refused = next(
            (code for word, code in self.exit_codes.items() if word in command), 0
        )
        if refused != 0:
            return SeatCommandResult(refused, "refused by the fake host")
        if "new-session" in command:
            self.session_alive = True
            self.scope_active = True
        if "kill-session" in command:
            self.session_alive = False
        if "stop" in command:
            self.scope_active = False
        return SeatCommandResult(0)

    def locate_executable(self, program: str) -> Path | None:
        return self.programs.get(program)

    def loopback_port_is_free(self, port: int) -> bool:
        return self.free_port

    def commands_containing(self, word: str) -> list[tuple[str, ...]]:
        return [command for command in self.commands if word in command]


def settings_for(
    tmp_path: Path,
    *,
    project_id: ProjectId = PROJECT_ID,
    database_path: str = DATABASE_PATH,
    project_root: Path | None = None,
    claude_executable: Path = Path("/opt/bin/claude"),
    mcp_document: SeatMcpDocument = MCP_DOCUMENT,
    port: int = SEAT_PORT,
) -> TerminalSeatSettings:
    root = project_root if project_root is not None else tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    return TerminalSeatSettings(
        project_id=project_id,
        project_root=root,
        database_path=database_path,
        state_directory=tmp_path / "state",
        tmux_executable=Path("/usr/bin/tmux"),
        ttyd_executable=Path("/usr/bin/ttyd"),
        claude_executable=claude_executable,
        mcp_document=mcp_document,
        port=port,
    )


def test_socket_session_and_scope_carry_one_seat_digest(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    digest = digest_of(DATABASE_PATH, PROJECT_ID.value)

    assert settings.socket_name == f"atelier-seat-{digest}"
    assert settings.session_name == f"seat-{digest}"
    assert settings.scope_unit == f"atelier2-seat-{digest}.scope"


def test_no_two_seats_share_a_tmux_server(tmp_path: Path) -> None:
    seat = settings_for(tmp_path)
    other_project = settings_for(tmp_path, project_id=ProjectId("anderes projekt"))
    other_deployment = settings_for(tmp_path, database_path="/tmp/e2e/atelier.db")

    sockets = {
        seat.socket_name,
        other_project.socket_name,
        other_deployment.socket_name,
    }
    assert len(sockets) == 3
    assert seat.scope_unit != other_project.scope_unit
    assert seat.session_name != other_deployment.session_name


def test_a_seat_port_outside_the_port_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"1\.\.65535"):
        settings_for(tmp_path, port=70000)


def test_creating_a_seat_starts_tmux_inside_a_transient_scope(tmp_path: Path) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)

    outcome = TerminalSeat(settings, host).ensure_session()

    assert outcome is TerminalSeatOutcome.CREATED
    assert host.commands_containing("new-session") == [
        (
            "/usr/bin/systemd-run",
            "--user",
            "--scope",
            "--collect",
            f"--unit={settings.unit_name}",
            "/usr/bin/tmux",
            "-L",
            settings.socket_name,
            "new-session",
            "-d",
            "-s",
            settings.session_name,
            "-c",
            str(settings.project_root),
        )
    ]


def test_the_agent_is_typed_into_the_login_shell(tmp_path: Path) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)

    TerminalSeat(settings, host).ensure_session()

    typed, entered = host.commands_containing("send-keys")
    assert typed[:-1] == (
        "/usr/bin/tmux",
        "-L",
        settings.socket_name,
        "send-keys",
        "-t",
        f"={settings.session_name}",
        "-l",
    )
    assert shlex.split(typed[-1]) == [
        "/opt/bin/claude",
        "--mcp-config",
        str(settings.state_directory / MCP_DOCUMENT.file_name),
    ]
    assert entered[-1] == "Enter"


def test_a_path_with_spaces_and_metacharacters_survives_the_hand_over(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost()
    claude = tmp_path / "agent tools" / "cl$aude;rm -rf /"
    settings = settings_for(tmp_path, claude_executable=claude)

    TerminalSeat(settings, host).ensure_session()

    typed = host.commands_containing("-l")[0]
    assert shlex.split(typed[-1])[0] == str(claude)


def test_a_running_seat_is_found_rather_than_created_again(tmp_path: Path) -> None:
    host = FakeSeatHost()
    seat = TerminalSeat(settings_for(tmp_path), host)
    seat.ensure_session()

    outcome = seat.ensure_session()

    assert outcome is TerminalSeatOutcome.ALREADY_RUNNING
    assert len(host.commands_containing("new-session")) == 1
    assert len(host.commands_containing("send-keys")) == 2


def test_an_orphaned_scope_is_stopped_before_the_seat_is_recreated(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(session_alive=False, scope_active=True)
    settings = settings_for(tmp_path)

    outcome = TerminalSeat(settings, host).ensure_session()

    assert outcome is TerminalSeatOutcome.RECREATED_AFTER_ORPHANED_SCOPE
    stopped = host.commands_containing("stop")
    assert stopped == [("/usr/bin/systemctl", "--user", "stop", settings.scope_unit)]
    assert host.commands.index(stopped[0]) < host.commands.index(
        host.commands_containing("new-session")[0]
    )


@pytest.mark.parametrize(
    "host_with_unreadable_probe",
    [
        pytest.param(
            lambda: FakeSeatHost(
                session_alive=True, scope_active=True, tmux_probe_unreadable=True
            ),
            id="tmux",
        ),
        pytest.param(
            lambda: FakeSeatHost(
                session_alive=True, scope_active=True, scope_probe_unreadable=True
            ),
            id="systemd",
        ),
    ],
)
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(TerminalSeat.ensure_session, id="ensure"),
        pytest.param(TerminalSeat.stop_session, id="stop"),
    ],
)
def test_a_probe_that_did_not_answer_ends_the_call_without_touching_anything(
    tmp_path: Path,
    host_with_unreadable_probe: Callable[[], FakeSeatHost],
    call: Callable[[TerminalSeat], TerminalSeatOutcome],
) -> None:
    host = host_with_unreadable_probe()

    with pytest.raises(TerminalSeatCommandFailed, match="could not be probed"):
        call(TerminalSeat(settings_for(tmp_path), host))

    assert host.commands_containing("stop") == []
    assert host.commands_containing("kill-session") == []
    assert host.commands_containing("new-session") == []


def test_a_seat_whose_project_root_is_gone_is_reported_without_being_killed(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(session_alive=True, scope_active=True)
    settings = settings_for(tmp_path, project_root=tmp_path / "gone")
    settings.project_root.rmdir()

    outcome = TerminalSeat(settings, host).ensure_session()

    assert outcome is TerminalSeatOutcome.UNUSABLE_PROJECT_ROOT_MISSING
    assert host.commands == []


def test_a_machine_without_systemd_run_is_refused_without_a_fallback_child(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(programs={"systemctl": Path("/usr/bin/systemctl")})

    outcome = TerminalSeat(settings_for(tmp_path), host).ensure_session()

    assert outcome is TerminalSeatOutcome.REFUSED_SYSTEMD_MISSING
    assert host.commands == []


def test_a_busy_seat_port_is_refused_rather_than_moved(tmp_path: Path) -> None:
    host = FakeSeatHost(free_port=False)

    outcome = TerminalSeat(settings_for(tmp_path), host).ensure_session()

    assert outcome is TerminalSeatOutcome.REFUSED_PORT_BUSY
    assert host.commands == []


def test_a_failing_management_command_is_not_swallowed(tmp_path: Path) -> None:
    host = FakeSeatHost(exit_codes={"new-session": 1})

    with pytest.raises(TerminalSeatCommandFailed, match="exited 1"):
        TerminalSeat(settings_for(tmp_path), host).ensure_session()


def test_a_seat_whose_agent_could_not_be_handed_over_is_discarded(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(exit_codes={"-l": 1})
    settings = settings_for(tmp_path)
    seat = TerminalSeat(settings, host)

    with pytest.raises(TerminalSeatCommandFailed) as refusal:
        seat.ensure_session()

    assert "could not be handed over" in str(refusal.value)
    assert "session and scope discarded" in str(refusal.value)
    assert "exited 1" in str(refusal.value.__cause__)
    assert host.commands_containing("kill-session") == [
        (
            "/usr/bin/tmux",
            "-L",
            settings.socket_name,
            "kill-session",
            "-t",
            f"={settings.session_name}",
        )
    ]
    assert host.commands_containing("stop") == [
        ("/usr/bin/systemctl", "--user", "stop", settings.scope_unit)
    ]
    host.exit_codes.clear()
    assert seat.ensure_session() is TerminalSeatOutcome.CREATED


def test_a_seat_that_could_not_be_discarded_says_what_it_left_behind(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(exit_codes={"-l": 1, "kill-session": 2})
    settings = settings_for(tmp_path)

    with pytest.raises(TerminalSeatCommandFailed) as refusal:
        TerminalSeat(settings, host).ensure_session()

    assert "left behind" in str(refusal.value)
    assert "kill-session" in str(refusal.value)
    assert "exited 2" in str(refusal.value)
    assert "exited 1" in str(refusal.value.__cause__)


def test_stopping_the_seat_kills_the_session_and_stops_its_scope(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)
    seat = TerminalSeat(settings, host)
    seat.ensure_session()
    host.commands.clear()

    outcome = seat.stop_session()

    assert outcome is TerminalSeatOutcome.STOPPED
    assert host.commands_containing("kill-session") == [
        (
            "/usr/bin/tmux",
            "-L",
            settings.socket_name,
            "kill-session",
            "-t",
            f"={settings.session_name}",
        )
    ]
    assert host.commands_containing("stop") == [
        ("/usr/bin/systemctl", "--user", "stop", settings.scope_unit)
    ]


def test_stopping_a_seat_that_is_not_running_ends_nothing(tmp_path: Path) -> None:
    host = FakeSeatHost()

    outcome = TerminalSeat(settings_for(tmp_path), host).stop_session()

    assert outcome is TerminalSeatOutcome.NOT_RUNNING
    assert host.commands_containing("kill-session") == []
    assert host.commands_containing("stop") == []


def test_the_ttyd_child_serves_loopback_writable_and_origin_checked(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    seat = TerminalSeat(settings, FakeSeatHost())

    assert seat.ttyd_command() == (
        "/usr/bin/ttyd",
        "-i",
        "127.0.0.1",
        "-p",
        str(SEAT_PORT),
        "-W",
        "-O",
        "-b",
        seat.base_path,
        "/usr/bin/tmux",
        "-L",
        settings.socket_name,
        "attach-session",
        "-t",
        f"={settings.session_name}",
    )


def test_the_ttyd_child_asks_for_no_credential(tmp_path: Path) -> None:
    command = TerminalSeat(settings_for(tmp_path), FakeSeatHost()).ttyd_command()

    assert "-c" not in command
    assert "-H" not in command


def test_every_seat_draws_its_own_unguessable_base_path(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    first = TerminalSeat(settings, FakeSeatHost())
    second = TerminalSeat(settings, FakeSeatHost())

    assert first.base_path != second.base_path
    assert re.fullmatch(r"/seat-[A-Za-z0-9_-]{24,}", first.base_path)
    assert first.url == f"http://127.0.0.1:{SEAT_PORT}{first.base_path}/"


def test_the_seat_persists_the_document_it_was_given_and_composes_nothing(
    tmp_path: Path,
) -> None:
    given = SeatMcpDocument("seat-mcp.json", "not the json grammar of any agent")
    settings = settings_for(tmp_path, mcp_document=given)

    TerminalSeat(settings, FakeSeatHost()).ensure_session()

    document = settings.state_directory / given.file_name
    assert document.read_text(encoding="utf-8") == given.content
    assert document.stat().st_mode & 0o777 == 0o600
    assert list(settings.project_root.iterdir()) == []
    assert list(settings.state_directory.iterdir()) == [document]


def test_an_older_document_is_replaced_without_keeping_its_permissions(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    document = settings.state_directory / MCP_DOCUMENT.file_name
    document.parent.mkdir(parents=True)
    document.write_text("the seat of an earlier serve", encoding="utf-8")
    document.chmod(0o644)

    TerminalSeat(settings, FakeSeatHost()).ensure_session()

    assert document.read_text(encoding="utf-8") == MCP_DOCUMENT.content
    assert document.stat().st_mode & 0o777 == 0o600


def test_a_link_where_the_document_belongs_is_replaced_rather_than_followed(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    elsewhere = tmp_path / "somebody-elses.json"
    elsewhere.write_text("not the seat's to write", encoding="utf-8")
    settings.state_directory.mkdir(parents=True)
    document = settings.state_directory / MCP_DOCUMENT.file_name
    document.symlink_to(elsewhere)

    TerminalSeat(settings, FakeSeatHost()).ensure_session()

    assert elsewhere.read_text(encoding="utf-8") == "not the seat's to write"
    assert not document.is_symlink()
    assert document.read_text(encoding="utf-8") == MCP_DOCUMENT.content


def test_a_document_that_could_not_be_placed_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path)

    def refuse_to_replace(*_: object, **__: object) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(os, "replace", refuse_to_replace)

    with pytest.raises(OSError, match="the disk went away"):
        TerminalSeat(settings, FakeSeatHost()).ensure_session()

    assert list(settings.state_directory.iterdir()) == []


def test_a_state_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    settings = settings_for(tmp_path)
    settings.state_directory.symlink_to(elsewhere)

    with pytest.raises(TerminalSeatCommandFailed, match="state directory"):
        TerminalSeat(settings, FakeSeatHost()).ensure_session()

    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(
    ("file_name", "content", "refusal"),
    [
        pytest.param("../escape.json", "{}", "single file name", id="escaping-name"),
        pytest.param("", "{}", "single file name", id="empty-name"),
        pytest.param("seat.json", "", "must not be empty", id="empty-content"),
    ],
)
def test_an_mcp_document_that_could_not_be_placed_safely_is_refused(
    file_name: str, content: str, refusal: str
) -> None:
    with pytest.raises(ValueError, match=refusal):
        SeatMcpDocument(file_name, content)


def test_no_seat_command_ever_reads_the_terminal(tmp_path: Path) -> None:
    host = FakeSeatHost()
    seat = TerminalSeat(settings_for(tmp_path), host)
    seat.ensure_session()
    seat.stop_session()

    issued = {argument for command in host.commands for argument in command}
    assert {"pipe-pane", "capture-pane"}.isdisjoint(issued)
