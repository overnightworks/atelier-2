"""The terminal seat's lifecycle: one tmux server per seat, outside the serve.

A seat is a tmux server of its own, running the agent CLI in one session,
held by a transient systemd user scope so that stopping the serve unit cannot
take it down, and reached through a ttyd child that the serve owns. Both
halves live here because they are one lifecycle: `ensure_session` and
`stop_session` decide about the session that outlives the serve, `ttyd_command`
builds the child the serve starts and stops with itself.

Socket, session, and scope carry one and the same digest, taken over this
deployment's database path and the project id together: a tmux server belongs
to exactly one socket, so a seat sharing its socket with another seat would
lose its session whenever that other seat's scope was stopped.

Nothing here reads, forwards, or stores terminal content: no `pipe-pane`, no
`capture-pane`, no seat bytes into journal, events, or receipts. The commands
composed here are management commands, and only their exit code and their own
error channel are read.
"""

from __future__ import annotations

import os
import secrets
import shlex
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import NoReturn, Protocol

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.host.address import DEFAULT_HOST, loopback_service_url

DEFAULT_SEAT_PORT = 7681
SEAT_PATH_TOKEN_BYTES = 24
SEAT_STAGED_NAME_BYTES = 8
HIGHEST_PORT = 65535
SEAT_DOCUMENT_MODE = 0o600
SEAT_DIGEST_SEPARATOR = b"\0"
STATE_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
STAGED_DOCUMENT_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
SYSTEMD_RUN_PROGRAM = "systemd-run"
SYSTEMCTL_PROGRAM = "systemctl"
SCOPE_UNIT_SUFFIX = ".scope"

# What tmux answers when the seat is simply not there. Every other failing
# probe is an unread answer, never an absence: an unreachable socket would
# otherwise read as "gone" and cost a living session its scope. These wordings
# are measured against the installed tmux when the seat gets its caller; an
# answer this list does not know stays a failed probe.
TMUX_ABSENT_ANSWERS = ("no server running", "can't find session", "session not found")
# `systemctl is-active` answers 3 for a unit that is not active and 4 for one it
# does not know; an unreachable bus answers otherwise.
SYSTEMCTL_ABSENT_EXIT_CODES = frozenset({3, 4})


class TerminalSeatCommandFailed(RuntimeError):
    """A step of the seat's lifecycle -- a command, or its state file -- failed."""


class SeatPresence(Enum):
    """What a probe established about one part of the seat."""

    ALIVE = "alive"
    MISSING = "missing"
    FAILED = "failed"


class TerminalSeatOutcome(Enum):
    """What one seat lifecycle call did, or why it did nothing."""

    CREATED = "created"
    ALREADY_RUNNING = "already-running"
    RECREATED_AFTER_ORPHANED_SCOPE = "recreated-after-orphaned-scope"
    STOPPED = "stopped"
    NOT_RUNNING = "not-running"
    UNUSABLE_PROJECT_ROOT_MISSING = "unusable-project-root-missing"
    REFUSED_SYSTEMD_MISSING = "refused-systemd-missing"
    REFUSED_PORT_BUSY = "refused-port-busy"


@dataclass(frozen=True, slots=True)
class SeatCommandResult:
    """What a management command answered: never terminal content."""

    exit_code: int
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class SeatMcpDocument:
    """The MCP configuration the composition serialized for this seat.

    The seat neither composes nor reads that grammar: whoever owns the agent
    CLI's configuration writes it, and the seat only puts it where the CLI is
    told to look.
    """

    file_name: str
    content: str

    def __post_init__(self) -> None:
        if not self.file_name or Path(self.file_name).name != self.file_name:
            raise ValueError(f"{self.file_name!r} is not a single file name")
        if not self.content:
            raise ValueError("a seat's MCP document must not be empty")


class TerminalSeatHost(Protocol):
    """The machine a seat lives on, as narrowly as the seat needs it."""

    def run(self, argv: Sequence[str]) -> SeatCommandResult: ...

    def locate_executable(self, program: str) -> Path | None: ...

    def loopback_port_is_free(self, port: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class TerminalSeatSettings:
    """Everything one project's seat is composed from."""

    project_id: ProjectId
    project_root: Path
    database_path: str
    state_directory: Path
    tmux_executable: Path
    ttyd_executable: Path
    claude_executable: Path
    mcp_document: SeatMcpDocument
    port: int = DEFAULT_SEAT_PORT

    def __post_init__(self) -> None:
        if not 1 <= self.port <= HIGHEST_PORT:
            raise ValueError(f"a seat port must be 1..{HIGHEST_PORT}, not {self.port}")

    @property
    def seat_digest(self) -> str:
        """The one identity of this seat: this deployment, this project."""

        return Sha256Hash.of(
            SEAT_DIGEST_SEPARATOR.join(
                (
                    self.database_path.encode("utf-8"),
                    self.project_id.value.encode("utf-8"),
                )
            )
        ).value

    @property
    def socket_name(self) -> str:
        """The tmux server of this seat, shared with no other seat."""

        return f"atelier-seat-{self.seat_digest}"

    @property
    def session_name(self) -> str:
        return f"seat-{self.seat_digest}"

    @property
    def unit_name(self) -> str:
        """The transient systemd user unit holding this seat's process tree."""

        return f"atelier2-seat-{self.seat_digest}"

    @property
    def scope_unit(self) -> str:
        return f"{self.unit_name}{SCOPE_UNIT_SUFFIX}"


@dataclass(frozen=True, slots=True)
class TerminalSeat:
    """One project's seat, addressed under a base path drawn for this seat."""

    settings: TerminalSeatSettings
    host: TerminalSeatHost
    path_token: str = field(
        init=False,
        default_factory=lambda: secrets.token_urlsafe(SEAT_PATH_TOKEN_BYTES),
    )

    @property
    def base_path(self) -> str:
        """Unreachable for a page that was never told it."""

        return f"/seat-{self.path_token}"

    @property
    def url(self) -> str:
        """Where a browser on this machine reaches the seat."""

        return loopback_service_url(self.settings.port, f"{self.base_path}/")

    def ensure_session(self) -> TerminalSeatOutcome:
        """Create the seat's session, or find the one that is already running."""

        if not self.settings.project_root.is_dir():
            return TerminalSeatOutcome.UNUSABLE_PROJECT_ROOT_MISSING
        systemd_run = self.host.locate_executable(SYSTEMD_RUN_PROGRAM)
        systemctl = self.host.locate_executable(SYSTEMCTL_PROGRAM)
        if systemd_run is None or systemctl is None:
            return TerminalSeatOutcome.REFUSED_SYSTEMD_MISSING
        if not self.host.loopback_port_is_free(self.settings.port):
            return TerminalSeatOutcome.REFUSED_PORT_BUSY
        session, scope = self._established_presence(systemctl)
        if session is SeatPresence.ALIVE:
            return TerminalSeatOutcome.ALREADY_RUNNING
        orphaned_scope = scope is SeatPresence.ALIVE
        if orphaned_scope:
            self._stop_scope(systemctl)
        self._create_session(systemd_run, systemctl)
        return (
            TerminalSeatOutcome.RECREATED_AFTER_ORPHANED_SCOPE
            if orphaned_scope
            else TerminalSeatOutcome.CREATED
        )

    def stop_session(self) -> TerminalSeatOutcome:
        """End the seat itself, which is what `atelier2 seat stop` means.

        Stopping the serve ends only the ttyd child; the session and the agent
        in it keep running in their own scope until this is called.
        """

        systemctl = self.host.locate_executable(SYSTEMCTL_PROGRAM)
        if systemctl is None:
            return TerminalSeatOutcome.REFUSED_SYSTEMD_MISSING
        session, scope = self._established_presence(systemctl)
        if session is SeatPresence.ALIVE:
            self._run_checked(
                self._tmux_command("kill-session", "-t", self._session_target)
            )
        if scope is SeatPresence.ALIVE:
            self._stop_scope(systemctl)
        if SeatPresence.ALIVE in (session, scope):
            return TerminalSeatOutcome.STOPPED
        return TerminalSeatOutcome.NOT_RUNNING

    def ttyd_command(self) -> tuple[str, ...]:
        """The serve's ttyd child: loopback, writable, origin-checked, no login.

        It binds the loopback host unconditionally, where the API's host is a
        setting: a pty is a shell, so this machine is the whole trust boundary.
        `-O` refuses a WebSocket whose origin is not ttyd's own page, which is
        the origin the seat's iframe carries; the drawn base path is what a page
        that was never told it cannot reach, since a rebound name passes the
        origin check. `-c` and `-H` stay absent: the seat asks for no password.
        """

        return (
            str(self.settings.ttyd_executable),
            "-i",
            DEFAULT_HOST,
            "-p",
            str(self.settings.port),
            "-W",
            "-O",
            "-b",
            self.base_path,
            *self._tmux_command("attach-session", "-t", self._session_target),
        )

    @property
    def _session_target(self) -> str:
        """The exact-name form, so no other session answers for this seat."""

        return f"={self.settings.session_name}"

    def _tmux_command(self, *arguments: str) -> tuple[str, ...]:
        return (
            str(self.settings.tmux_executable),
            "-L",
            self.settings.socket_name,
            *arguments,
        )

    def _session_presence(self) -> SeatPresence:
        answer = self.host.run(
            self._tmux_command("has-session", "-t", self._session_target)
        )
        if answer.exit_code == 0:
            return SeatPresence.ALIVE
        if any(absent in answer.stderr for absent in TMUX_ABSENT_ANSWERS):
            return SeatPresence.MISSING
        return SeatPresence.FAILED

    def _scope_presence(self, systemctl: Path) -> SeatPresence:
        answer = self.host.run(
            (str(systemctl), "--user", "is-active", self.settings.scope_unit)
        )
        if answer.exit_code == 0:
            return SeatPresence.ALIVE
        if answer.exit_code in SYSTEMCTL_ABSENT_EXIT_CODES:
            return SeatPresence.MISSING
        return SeatPresence.FAILED

    def _established_presence(
        self, systemctl: Path
    ) -> tuple[SeatPresence, SeatPresence]:
        """Both halves of the seat, read before any of them is touched.

        A probe that did not answer ends the call before the first mutation:
        an absence the seat could not confirm is not an absence, and a session
        must not be killed on the strength of a scope nobody could read.
        """

        session = self._session_presence()
        scope = self._scope_presence(systemctl)
        if SeatPresence.FAILED in (session, scope):
            raise TerminalSeatCommandFailed(
                f"the seat {self.settings.seat_digest} could not be probed"
            )
        return session, scope

    def _stop_scope(self, systemctl: Path) -> None:
        self._run_checked((str(systemctl), "--user", "stop", self.settings.scope_unit))

    def _create_session(self, systemd_run: Path, systemctl: Path) -> None:
        configuration = self._persist_mcp_document()
        self._run_checked(
            (
                str(systemd_run),
                "--user",
                "--scope",
                "--collect",
                f"--unit={self.settings.unit_name}",
                *self._tmux_command(
                    "new-session",
                    "-d",
                    "-s",
                    self.settings.session_name,
                    "-c",
                    str(self.settings.project_root),
                ),
            )
        )
        try:
            self._type_into_login_shell(
                (
                    str(self.settings.claude_executable),
                    "--mcp-config",
                    str(configuration),
                )
            )
        except TerminalSeatCommandFailed as hand_over:
            self._discard_half_created_seat(systemctl, hand_over)

    def _discard_half_created_seat(
        self, systemctl: Path, hand_over: TerminalSeatCommandFailed
    ) -> NoReturn:
        """Leave no seat without its agent, and report what the cleanup left.

        The hand-over failure stays the cause; a teardown command that failed
        too is named beside it rather than raised over it, because a seat
        nobody could discard is what the next call would otherwise find.
        """

        teardown = (
            self._tmux_command("kill-session", "-t", self._session_target),
            (str(systemctl), "--user", "stop", self.settings.scope_unit),
        )
        left_behind: list[str] = []
        for argv in teardown:
            answer = self.host.run(argv)
            if answer.exit_code != 0:
                left_behind.append(f"{shlex.join(argv)} exited {answer.exit_code}")
        cleanup = (
            "session and scope discarded"
            if not left_behind
            else f"left behind: {'; '.join(left_behind)}"
        )
        raise TerminalSeatCommandFailed(
            f"the seat's agent could not be handed over ({hand_over}); {cleanup}"
        ) from hand_over

    def _type_into_login_shell(self, argv: Sequence[str]) -> None:
        """Type the agent CLI where the operator would type it.

        The CLI is not the session's command: ending it leaves the login shell
        standing with its prompt, and nothing restarts it.
        """

        self._run_checked(
            self._tmux_command(
                "send-keys", "-t", self._session_target, "-l", shlex.join(argv)
            )
        )
        self._run_checked(
            self._tmux_command("send-keys", "-t", self._session_target, "Enter")
        )

    def _persist_mcp_document(self) -> Path:
        """Put the composed MCP document where the serve keeps its state.

        Never into the operator's project tree, and readable only by the user
        whose agent is about to be told to read it: the document is written to
        a fresh file of its own and moved onto its name, so no reader ever sees
        a half-written seat, an older file's permissions, or a link somebody
        put in the way.
        """

        self.settings.state_directory.mkdir(parents=True, exist_ok=True)
        state_directory = self._opened_state_directory()
        try:
            self._replace_document_from_staged(state_directory)
        finally:
            os.close(state_directory)
        return self.settings.state_directory / self.settings.mcp_document.file_name

    def _opened_state_directory(self) -> int:
        """The state directory itself, held open while the document is placed.

        Every step below works relative to this descriptor, so a directory
        swapped for a link between the steps cannot redirect the write.
        """

        try:
            return os.open(self.settings.state_directory, STATE_DIRECTORY_FLAGS)
        except OSError as error:
            raise TerminalSeatCommandFailed(
                f"the seat's state directory {self.settings.state_directory} "
                "is not a directory this serve can open"
            ) from error

    def _replace_document_from_staged(self, state_directory: int) -> None:
        """Write the document beside its name and move it on, or leave nothing."""

        document = self.settings.mcp_document
        staged_name = (
            f"{document.file_name}.{secrets.token_hex(SEAT_STAGED_NAME_BYTES)}"
        )
        descriptor = os.open(
            staged_name,
            STAGED_DOCUMENT_FLAGS,
            SEAT_DOCUMENT_MODE,
            dir_fd=state_directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as opened:
                opened.write(document.content)
                opened.flush()
                os.fsync(opened.fileno())
            os.replace(
                staged_name,
                document.file_name,
                src_dir_fd=state_directory,
                dst_dir_fd=state_directory,
            )
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(staged_name, dir_fd=state_directory)
            raise

    def _run_checked(self, argv: Sequence[str]) -> None:
        answer = self.host.run(argv)
        if answer.exit_code != 0:
            raise TerminalSeatCommandFailed(
                f"{shlex.join(argv)} exited {answer.exit_code}: {answer.stderr}"
            )
