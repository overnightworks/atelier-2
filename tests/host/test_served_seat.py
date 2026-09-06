from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.claude_executable import ClaudeExecutable
from atelier2.api.app import create_app
from atelier2.api.openapi import SEAT_PATH
from atelier2.api.seat import SeatState
from atelier2.contracts.host_configuration import ProjectId
from atelier2.host import main, seat_command
from atelier2.host.seat_command import STOPPED_REPORT
from atelier2.host.served_seat import (
    SeatDeclaration,
    ServedSeat,
    seat_lifespan,
    seat_mcp_document,
    seat_settings,
)
from atelier2.host.terminal_seat import TerminalSeat
from tests.host.test_local_host import served_settings
from tests.host.test_terminal_seat import FakeSeatHost
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

PROJECT = ProjectId("atelier zwei")
SERVICE_URL = "http://127.0.0.1:8422"
SEAT_PORT = 7691
DATABASE_NAME = "durable.sqlite"


def _program(path: Path, body: str = "exit 0") -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def declared_seat(
    tmp_path: Path, *, terminal_runs: str = "exec sleep 30"
) -> SeatDeclaration:
    """A seat whose three programs are this test's own, not the machine's."""

    return SeatDeclaration(
        _program(tmp_path / "tmux"),
        _program(tmp_path / "ttyd", terminal_runs),
        ClaudeExecutable(_program(tmp_path / "claude")),
        SEAT_PORT,
    )


def served_seat(
    tmp_path: Path,
    *,
    host: FakeSeatHost | None = None,
    database_name: str = DATABASE_NAME,
    project_id: ProjectId = PROJECT,
    terminal_runs: str = "exec sleep 30",
) -> ServedSeat:
    project_root = tmp_path / "project"
    project_root.mkdir(exist_ok=True)
    settings = seat_settings(
        declared_seat(tmp_path, terminal_runs=terminal_runs),
        project_id=project_id,
        project_root=project_root,
        database_path=tmp_path / database_name,
        service_url=SERVICE_URL,
    )
    return ServedSeat(TerminalSeat(settings, host or FakeSeatHost()), project_id)


def seat_door(seat: ServedSeat) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            seat_reader=seat.reading,
            lifespan=seat_lifespan(seat, None),
        )
    )


def test_the_workbench_is_told_where_this_serve_terminal_answers(
    tmp_path: Path,
) -> None:
    seat = served_seat(tmp_path)

    with seat_door(seat) as client:
        answered = client.get(SEAT_PATH).json()

    assert answered["state"] == SeatState.ALIVE.value
    assert answered["project_id"] == PROJECT.value
    assert answered["url"].startswith(f"http://127.0.0.1:{SEAT_PORT}/seat-")


def test_the_seat_terminal_ends_with_the_serve_that_opened_it(tmp_path: Path) -> None:
    seat = served_seat(tmp_path)

    with seat_door(seat) as client:
        client.get(SEAT_PATH)

    assert seat.reading().state is SeatState.FAILED
    assert seat.reading().url is None


def test_a_machine_without_systemd_leaves_the_rest_of_the_workbench_serving(
    tmp_path: Path,
) -> None:
    machine = FakeSeatHost(programs={})
    seat = served_seat(tmp_path, host=machine)

    with seat_door(seat) as client:
        answered = client.get(SEAT_PATH).json()
        health = client.get("/atelier/api/v1/health")

    assert answered["state"] == SeatState.FAILED.value
    assert health.status_code == 200
    assert machine.commands_containing("new-session") == []


def test_two_deployments_of_this_machine_never_share_one_seat(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    declaration = declared_seat(tmp_path)
    live, harness = (
        seat_settings(
            declaration,
            project_id=PROJECT,
            project_root=project_root,
            database_path=tmp_path / database_name,
            service_url=SERVICE_URL,
        )
        for database_name in (DATABASE_NAME, "e2e.sqlite")
    )

    assert live.socket_name != harness.socket_name
    assert live.session_name != harness.session_name
    assert live.scope_unit != harness.scope_unit


def test_the_seat_agent_is_configured_with_this_serve_own_loopback_door() -> None:
    document = json.loads(seat_mcp_document(SERVICE_URL).content)

    (server,) = document["mcpServers"].values()
    assert server["args"][-2:] == ["--service", SERVICE_URL]
    assert SERVICE_URL in json.dumps(document)
    assert "token" not in json.dumps(document).lower()


def test_a_declared_seat_without_its_project_refuses_the_serve(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--project-id"):
        served_settings(tmp_path, terminal_seat=declared_seat(tmp_path))


def test_stopping_the_seat_ends_the_session_the_serve_left_running(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = FakeSeatHost(session_alive=True, scope_active=True)
    monkeypatch.setattr(seat_command, "LocalSeatMachine", lambda: machine)
    declaration = declared_seat(tmp_path)

    exit_code = main(
        [
            "seat",
            "stop",
            "--database",
            str(tmp_path / DATABASE_NAME),
            "--project-id",
            PROJECT.value,
            "--project-root",
            str(tmp_path),
            "--claude-executable",
            str(declaration.claude_executable.path),
            "--seat-tmux-executable",
            str(declaration.tmux_executable),
            "--seat-ttyd-executable",
            str(declaration.ttyd_executable),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == STOPPED_REPORT
    assert machine.commands_containing("kill-session") != []
    assert machine.commands_containing("stop") != []


def test_a_seat_whose_terminal_program_is_not_there_refuses_the_start(
    tmp_path: Path,
) -> None:
    """The seat starts from the named path or not at all; it searches nowhere."""

    declaration = declared_seat(tmp_path)

    with pytest.raises(ValueError, match="--seat-ttyd-executable"):
        SeatDeclaration(
            declaration.tmux_executable,
            tmp_path / "no-ttyd-here",
            declaration.claude_executable,
        )
