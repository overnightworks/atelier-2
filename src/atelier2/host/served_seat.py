"""The seat this serve holds: what it is declared from, and what it answers.

`terminal_seat.py` owns the seat's lifecycle -- one tmux server per seat in its
own systemd scope, and the ttyd child that attaches to it. This module is that
lifecycle's composition: the values an operator declares a seat with, the
machine those commands actually run on, and the holder the serve opens at
startup, closes at shutdown, and the API door reads while it serves.

Stopping the serve ends the ttyd child only. The session and the agent in it
keep running in their own scope until `atelier2 seat stop` ends them, so a
redeploy leaves the operator in the same conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from starlette.types import Lifespan

from atelier2.adapters.claude_executable import ClaudeExecutable
from atelier2.api.seat import SeatReading, SeatState
from atelier2.contracts.host_configuration import ProjectId
from atelier2.host.address import DEFAULT_HOST, is_loopback_host
from atelier2.host.mcp_command import stdio_door_command
from atelier2.host.mcp_tools import MCP_SERVER_NAME
from atelier2.host.terminal_seat import (
    DEFAULT_SEAT_PORT,
    SeatCommandResult,
    SeatMcpDocument,
    TerminalSeat,
    TerminalSeatCommandFailed,
    TerminalSeatOutcome,
    TerminalSeatSettings,
)

SEAT_STATE_DIRECTORY_NAME = "terminal-seat"
SEAT_MCP_DOCUMENT_NAME = "mcp.json"
# A management command that has not answered by now is a failure, not a slow
# machine: every one of them is a tmux or systemd call about a socket on this
# host. Without a bound, a hung one would hold the serve's startup forever.
SEAT_COMMAND_TIMEOUT_SECONDS = 20.0
# What the ttyd child gets to close its sockets before it is killed.
TERMINAL_TERMINATION_GRACE_SECONDS = 5.0

SEAT_REFUSALS = {
    TerminalSeatOutcome.UNUSABLE_PROJECT_ROOT_MISSING: (
        "the seat's project root is not a directory this serve can open"
    ),
    TerminalSeatOutcome.REFUSED_SYSTEMD_MISSING: (
        "the seat needs a systemd user instance to hold its session outside "
        "this serve, and this machine has none"
    ),
    TerminalSeatOutcome.REFUSED_PORT_BUSY: (
        "the seat's loopback port is already taken by another process"
    ),
}


@dataclass(frozen=True, slots=True)
class SeatDeclaration:
    """The values an operator declares one serve's terminal seat with.

    The agent CLI comes from its own owner (`--claude-executable`), which is
    also the headless deployment's value: a seat runs the same binary
    interactively, and none of that deployment's own rules.
    """

    tmux_executable: Path
    ttyd_executable: Path
    claude_executable: ClaudeExecutable
    port: int = DEFAULT_SEAT_PORT

    def __post_init__(self) -> None:
        for named, executable in (
            ("--seat-tmux-executable", self.tmux_executable),
            ("--seat-ttyd-executable", self.ttyd_executable),
        ):
            resolved = executable.resolve()
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                raise ValueError(f"{named} must name an existing executable file")


class LocalSeatMachine:
    """This machine, as narrowly as a seat needs it.

    Only the management commands' own exit code and error channel are read.
    Terminal content never passes through here: the ttyd child the serve starts
    inherits this process's own streams, so its own startup complaints stay
    readable in the journal while the session's bytes stay in the session.
    """

    def run(self, argv: Sequence[str]) -> SeatCommandResult:
        finished = subprocess.run(
            tuple(argv),
            capture_output=True,
            text=True,
            timeout=SEAT_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        return SeatCommandResult(finished.returncode, finished.stderr)

    def locate_executable(self, program: str) -> Path | None:
        found = shutil.which(program)
        return None if found is None else Path(found)

    def loopback_port_is_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((DEFAULT_HOST, port))
            except OSError:
                return False
        return True


class ServedSeat:
    """One serve's seat: the session it finds, and the terminal server it owns.

    A seat that could not be opened refuses softly and says so through the
    door: the rest of the workbench -- the open questions, the runs -- is
    readable without a terminal, and a room that fell over with its seat would
    take the operator's decisions down with it.
    """

    def __init__(self, seat: TerminalSeat, project_id: ProjectId) -> None:
        self._seat = seat
        self._project_id = project_id
        self._terminal: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        """Find or create this seat's session, then serve a terminal on it."""

        try:
            outcome = self._seat.ensure_session()
        except TerminalSeatCommandFailed as failure:
            self._refuse(str(failure))
            return
        refusal = SEAT_REFUSALS.get(outcome)
        if refusal is not None:
            self._refuse(refusal)
            return
        self._terminal = subprocess.Popen(self._seat.ttyd_command())

    def close(self) -> None:
        """End the terminal server. The session and its agent keep running."""

        terminal, self._terminal = self._terminal, None
        if terminal is None:
            return
        terminal.terminate()
        try:
            terminal.wait(TERMINAL_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            terminal.kill()
            terminal.wait(TERMINAL_TERMINATION_GRACE_SECONDS)

    def reading(self) -> SeatReading:
        """What the cockpit is told right now: an address, or a refusal."""

        terminal = self._terminal
        if terminal is None or terminal.poll() is not None:
            return SeatReading(SeatState.FAILED)
        return SeatReading(
            SeatState.ALIVE, url=self._seat.url, project_id=self._project_id.value
        )

    def _refuse(self, reason: str) -> None:
        """Say once, where the serve is read, why this seat has no terminal."""

        logging.getLogger("atelier2").error("atelier2 terminal seat: %s", reason)


def refuse_unservable_seat(
    declaration: SeatDeclaration | None,
    *,
    project_id: ProjectId | None,
    project_root: Path | None,
    api_host: str,
) -> None:
    """Refuse a declared seat this deployment must not open, before it opens.

    A seat is one project's, opened where that project lies. And it is a shell
    without a login: its door hands out the address and the drawn path that are
    all a browser needs, so a deployment that answers off loopback would be
    publishing them to the network.
    """

    if declaration is None:
        return
    if project_id is None or project_root is None:
        raise ValueError(
            "a terminal seat is one project's seat, opened where that project "
            "lies, so it needs --project-id and --project-root"
        )
    if not is_loopback_host(api_host):
        raise ValueError(
            f"serving a terminal seat requires a loopback bind, not "
            f"{api_host!r}: the seat's door hands out the address and the drawn "
            "path of a shell that asks for no password"
        )


def seat_mcp_document(service_url: str) -> SeatMcpDocument:
    """The MCP configuration the seat's agent CLI is started with.

    The serve composes it and hands it to the CLI on its command line; nothing
    is written into the operator's project tree, and the document names this
    deployment's own loopback door and no other server.
    """

    command = stdio_door_command(service_url)
    return SeatMcpDocument(
        SEAT_MCP_DOCUMENT_NAME,
        json.dumps(
            {
                "mcpServers": {
                    MCP_SERVER_NAME: {
                        "command": command[0],
                        "args": list(command[1:]),
                    }
                }
            },
            separators=(",", ":"),
        ),
    )


def seat_settings(
    declaration: SeatDeclaration,
    *,
    project_id: ProjectId,
    project_root: Path,
    database_path: Path,
    service_url: str,
) -> TerminalSeatSettings:
    """One seat, composed from the declaration and the project it belongs to."""

    return TerminalSeatSettings(
        project_id=project_id,
        project_root=project_root,
        database_path=str(database_path),
        state_directory=database_path.parent / SEAT_STATE_DIRECTORY_NAME,
        tmux_executable=declaration.tmux_executable,
        ttyd_executable=declaration.ttyd_executable,
        claude_executable=declaration.claude_executable.path,
        mcp_document=seat_mcp_document(service_url),
        port=declaration.port,
    )


def composed_seat(
    declaration: SeatDeclaration | None,
    *,
    project_id: ProjectId | None,
    project_root: Path | None,
    database_path: Path,
    service_url: str,
) -> ServedSeat | None:
    """A deployment's seat, composed but not yet opened.

    Its own lifespan opens it, because creating a session and starting a
    terminal server are the serving process's side effects, not the
    composition's -- a test that only builds an app starts no processes.
    """

    if declaration is None:
        return None
    if project_id is None or project_root is None:
        raise ValueError("a terminal seat needs the project it belongs to")
    return ServedSeat(
        TerminalSeat(
            seat_settings(
                declaration,
                project_id=project_id,
                project_root=project_root,
                database_path=database_path,
                service_url=service_url,
            ),
            LocalSeatMachine(),
        ),
        project_id,
    )


def seat_lifespan(
    seat: ServedSeat, inner: Lifespan[FastAPI] | None
) -> Lifespan[FastAPI]:
    """Open the seat when the application starts, close it when it stops."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(seat.open)
        try:
            if inner is None:
                yield
            else:
                async with inner(app):
                    yield
        finally:
            await asyncio.to_thread(seat.close)

    return lifespan
