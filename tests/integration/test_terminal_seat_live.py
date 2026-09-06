"""The seat against the real binaries: one tmux server, one scope, one terminal.

Skipped, named, wherever `tmux`, `ttyd` or a systemd user instance is missing
-- the pipeline's runner image carries none of the three, so this is the proof
the operator's own machine gives and the pipeline honestly cannot. The agent
CLI here is a script of this test's own: what the seat owes is a login shell
with the CLI typed into it, never a billed provider.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from atelier2.adapters.claude_executable import ClaudeExecutable
from atelier2.api.seat import SeatState
from atelier2.contracts.host_configuration import ProjectId
from atelier2.host.served_seat import (
    LocalSeatMachine,
    SeatDeclaration,
    ServedSeat,
    seat_settings,
)
from atelier2.host.terminal_seat import (
    TerminalSeat,
    TerminalSeatOutcome,
    TerminalSeatSettings,
)

SEAT_PROGRAMS = ("tmux", "ttyd", "systemd-run", "systemctl")
USER_BUS_VARIABLE = "XDG_RUNTIME_DIR"
PROJECT = ProjectId("atelier-2-seat-integration")
SERVICE_URL = "http://127.0.0.1:8422"
TERMINAL_READY_TIMEOUT_SECONDS = 10.0
TERMINAL_POLL_SECONDS = 0.1

_absent = [program for program in SEAT_PROGRAMS if shutil.which(program) is None]
if os.environ.get(USER_BUS_VARIABLE) is None:
    _absent.append("a systemd user instance")
pytestmark = pytest.mark.skipif(
    bool(_absent),
    reason=f"this machine has no seat to open: {', '.join(_absent)} missing",
)


@dataclass(frozen=True, slots=True)
class OpenedSeat:
    """The seat under test, beside the identity it was composed from."""

    served: ServedSeat
    settings: TerminalSeatSettings
    seat: TerminalSeat


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _agent_cli(tmp_path: Path) -> Path:
    """A CLI that keeps running and prints one line, as the real one does."""

    script = tmp_path / "seat-agent-cli"
    script.write_text("#!/bin/sh\necho 'atelier seat agent'\nexec sleep 600\n")
    script.chmod(0o755)
    return script


@pytest.fixture
def opened(tmp_path: Path) -> Iterator[OpenedSeat]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    settings = seat_settings(
        SeatDeclaration(
            Path(str(shutil.which("tmux"))),
            Path(str(shutil.which("ttyd"))),
            ClaudeExecutable(_agent_cli(tmp_path)),
            _free_port(),
        ),
        project_id=PROJECT,
        # Its own store path, so this seat's socket, session and scope are
        # this test's and never the live deployment's.
        project_root=project_root,
        database_path=tmp_path / "durable.sqlite",
        service_url=SERVICE_URL,
    )
    seat = TerminalSeat(settings, LocalSeatMachine())
    served = ServedSeat(seat, PROJECT)
    served.open()
    try:
        yield OpenedSeat(served, settings, seat)
    finally:
        served.close()
        seat.stop_session()


def _terminal_answers(url: str) -> bool:
    deadline = time.monotonic() + TERMINAL_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as answer:
                return answer.status == 200
        except (URLError, OSError):
            time.sleep(TERMINAL_POLL_SECONDS)
    return False


def _session_lives(settings: TerminalSeatSettings) -> bool:
    probe = subprocess.run(
        (
            str(settings.tmux_executable),
            "-L",
            settings.socket_name,
            "has-session",
            "-t",
            f"={settings.session_name}",
        ),
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def test_the_seat_opens_one_session_and_a_second_open_finds_it(
    opened: OpenedSeat,
) -> None:
    assert opened.served.reading().state is SeatState.ALIVE
    assert _session_lives(opened.settings)

    assert opened.seat.ensure_session() is TerminalSeatOutcome.ALREADY_RUNNING


def test_the_terminal_answers_at_the_address_the_seat_drew(
    opened: OpenedSeat,
) -> None:
    address = opened.served.reading().url

    assert address is not None
    assert _terminal_answers(address)


def test_stopping_the_serve_leaves_the_session_and_its_agent_running(
    opened: OpenedSeat,
) -> None:
    opened.served.close()

    assert _session_lives(opened.settings)
    assert opened.served.reading().state is SeatState.FAILED


def test_stopping_the_seat_ends_its_session(opened: OpenedSeat) -> None:
    assert opened.seat.stop_session() is TerminalSeatOutcome.STOPPED

    assert not _session_lives(opened.settings)
    assert opened.seat.stop_session() is TerminalSeatOutcome.NOT_RUNNING
