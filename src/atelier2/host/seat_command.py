"""The operator's two seat words: how a serve declares one, and how one ends.

A seat outlives the serve that opened it -- its session runs in a scope of its
own -- so ending it is a command rather than a side effect of stopping the
serve. Both words name the same seat the same way, which is why the flags live
here once and `serve` borrows them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atelier2.adapters.claude_executable import ClaudeExecutable
from atelier2.contracts.host_configuration import ProjectId
from atelier2.host.address import DEFAULT_SERVICE_URL
from atelier2.host.served_seat import LocalSeatMachine, SeatDeclaration, seat_settings
from atelier2.host.terminal_seat import (
    TerminalSeat,
    TerminalSeatCommandFailed,
    TerminalSeatOutcome,
)

SEAT_STOP_DESCRIPTION = """\
End this project's terminal seat: its tmux session, the agent CLI in it, and
the transient systemd user scope holding them.

The seat is named exactly as the serve named it -- the same store, the same
project, the same executables -- because its identity is drawn from those
values. Nothing else is stopped: a running serve keeps serving, and its next
start finds no session and opens a fresh one.
"""

STOPPED_REPORT = "terminal seat stopped"
NOT_RUNNING_REPORT = "no terminal seat was running"
SYSTEMD_MISSING_REPORT = (
    "this machine has no systemd user instance, so no seat scope can be read"
)


def add_seat_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """The values a terminal seat is declared with, for whoever names one."""

    parser.add_argument("--seat-tmux-executable", type=Path, required=required)
    parser.add_argument("--seat-ttyd-executable", type=Path, required=required)
    parser.add_argument("--seat-port", type=int)


def add_seat_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Declare `seat stop`, the one word that ends a seat."""

    parser = commands.add_parser("seat", help="end this project's terminal seat")
    operations = parser.add_subparsers(dest="seat_command", required=True)
    stop = operations.add_parser(
        "stop",
        help="end the seat's session, its agent, and its scope",
        description=SEAT_STOP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stop.add_argument("--database", type=Path, required=True)
    stop.add_argument("--project-id", required=True)
    stop.add_argument("--project-root", type=Path, required=True)
    stop.add_argument("--claude-executable", type=Path, required=True)
    add_seat_arguments(stop, required=True)


def declared_seat(parsed: argparse.Namespace) -> SeatDeclaration | None:
    """The seat this command line declares, or none.

    Both paths are named together or not at all: a seat without its terminal
    server is a session nobody can see, and one without tmux is nothing at all.
    The agent CLI comes from its own owner, so a declared seat needs it too.
    """

    named = (parsed.seat_tmux_executable, parsed.seat_ttyd_executable)
    if all(value is None for value in named):
        if parsed.seat_port is not None:
            raise ValueError(
                "--seat-port belongs to a declared seat, so it needs "
                "--seat-tmux-executable and --seat-ttyd-executable"
            )
        return None
    if any(value is None for value in named):
        raise ValueError(
            "a terminal seat needs --seat-tmux-executable and "
            "--seat-ttyd-executable together"
        )
    if parsed.claude_executable is None:
        raise ValueError(
            "a terminal seat runs the agent CLI in its session, so it needs "
            "--claude-executable"
        )
    port = {} if parsed.seat_port is None else {"port": parsed.seat_port}
    return SeatDeclaration(
        parsed.seat_tmux_executable,
        parsed.seat_ttyd_executable,
        ClaudeExecutable(parsed.claude_executable),
        **port,
    )


def execute_seat(parsed: argparse.Namespace) -> int:
    """End the named seat and say what was there, or why nothing could be read."""

    try:
        outcome = _stopped_seat(parsed)
    except (TerminalSeatCommandFailed, ValueError) as refusal:
        print(str(refusal), file=sys.stderr)
        return 1
    match outcome:
        case TerminalSeatOutcome.STOPPED:
            print(STOPPED_REPORT)
            return 0
        case TerminalSeatOutcome.NOT_RUNNING:
            print(NOT_RUNNING_REPORT)
            return 0
        case _:
            print(SYSTEMD_MISSING_REPORT, file=sys.stderr)
            return 1


def _stopped_seat(parsed: argparse.Namespace) -> TerminalSeatOutcome:
    declaration = declared_seat(parsed)
    if declaration is None:
        raise ValueError("stopping a terminal seat names the seat it ends")
    seat = TerminalSeat(
        seat_settings(
            declaration,
            project_id=ProjectId(parsed.project_id),
            project_root=parsed.project_root,
            database_path=parsed.database,
            # A stop reads identity only -- store, project, socket, scope --
            # and writes no configuration; the address is composed the way a
            # serve composes it so both words describe the same seat.
            service_url=DEFAULT_SERVICE_URL,
        ),
        LocalSeatMachine(),
    )
    return seat.stop_session()
