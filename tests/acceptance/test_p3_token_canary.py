"""A platform token crosses only the credential helper's file boundary."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import TracebackType
from typing import Self, cast
from urllib.parse import quote, urlsplit

import sqlalchemy as sa
from dbos import DBOS
from pytest import LogCaptureFixture

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, effect_receipts, run_events
from atelier2.adapters.dbos.workflow_ids import (
    bootstrap_workflow_id_for,
    node_workflow_id_for,
)
from atelier2.adapters.git_transport.effects import (
    GitCommandResult,
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import PushAtelierCommitReceipt
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunState
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.effects import EffectAdapterRegistration, EffectAdapterRegistry
from atelier2.ports.issue_observation import WorkItemRevisionObserved
from tests.acceptance.test_v3_push_before_open_pr import (
    _WRITE_CANDIDATE,
    AGENT_OUTPUT,
    ITEM,
    ORDER_NAME,
    PROJECT,
    RUN,
    _git,
    _publish,
    _repositories,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    launching,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.work_item_claims import fake_agent_claim_executable

_STANDARD_LOG_RECORD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__)


@dataclass
class RecordingRunner:
    trace_directory: Path
    calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = field(default_factory=list)
    child_process_records: list[bytes] = field(default_factory=list)
    standard_output_records: list[bytes] = field(default_factory=list)
    standard_error_records: list[bytes] = field(default_factory=list)

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        self.calls.append((arguments, dict(environment)))
        trace = self.trace_directory / f"git-{len(self.calls)}.trace"
        completed = subprocess.run(
            (
                "strace",
                "-f",
                "-v",
                "-s",
                "4096",
                "-e",
                "trace=execve",
                "-o",
                str(trace),
                "git",
                *arguments,
            ),
            cwd=working_directory,
            env=environment,
            input=standard_input,
            capture_output=True,
            check=False,
        )
        self.child_process_records.append(trace.read_bytes())
        self.standard_output_records.append(completed.stdout)
        self.standard_error_records.append(completed.stderr)
        return GitCommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


@dataclass(frozen=True)
class RequiredSqliteRow:
    table_name: str
    values: Mapping[str, object]


@dataclass
class GitHttpRemote:
    project_root: Path
    expected_authorization_digest: bytes
    _server: ThreadingHTTPServer | None = None
    _thread: Thread | None = None

    def __enter__(self) -> Self:
        remote = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self._serve_git()

            def do_POST(self) -> None:
                self._serve_git()

            def _serve_git(self) -> None:
                authorization_digest = hashlib.sha256(
                    self.headers.get("Authorization", "").encode("latin-1")
                ).digest()
                if authorization_digest != remote.expected_authorization_digest:
                    self.send_response(HTTPStatus.UNAUTHORIZED)
                    self.send_header("WWW-Authenticate", 'Basic realm="atelier-test"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                parsed = urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                assert remote._server is not None
                server_host, server_port = cast(
                    tuple[str, int], remote._server.server_address
                )
                completed = subprocess.run(
                    ("git", "http-backend"),
                    env={
                        **os.environ,
                        "GIT_PROJECT_ROOT": str(remote.project_root),
                        "GIT_HTTP_EXPORT_ALL": "1",
                        "PATH_INFO": parsed.path,
                        "QUERY_STRING": parsed.query,
                        "REQUEST_METHOD": self.command,
                        "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                        "CONTENT_LENGTH": str(length),
                        "REMOTE_USER": "x-access-token",
                        "SERVER_NAME": str(server_host),
                        "SERVER_PORT": str(server_port),
                        "SERVER_PROTOCOL": self.protocol_version,
                    },
                    input=body,
                    capture_output=True,
                    check=True,
                )
                header_bytes, separator, response_body = completed.stdout.partition(
                    b"\r\n\r\n"
                )
                assert separator
                status = HTTPStatus.OK
                response_headers: list[tuple[str, str]] = []
                for line in header_bytes.decode("ascii").split("\r\n"):
                    name, value = line.split(":", 1)
                    if name.lower() == "status":
                        status = HTTPStatus(int(value.strip().split(" ", 1)[0]))
                    else:
                        response_headers.append((name, value.strip()))
                self.send_response(status)
                for name, value in response_headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._server is not None
        assert self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/remote.git"


def _canary_encodings(canary: str) -> Mapping[str, bytes]:
    plaintext = canary.encode("utf-8")
    basic_credentials = base64.b64encode(b"x-access-token:" + plaintext)
    return {
        "plaintext": plaintext,
        "base64 credentials": basic_credentials,
        "Authorization Basic value": b"Basic " + basic_credentials,
        "URL encoded": quote(canary, safe="").encode("ascii"),
        "percent escaped": b"".join(f"%{byte:02X}".encode() for byte in plaintext),
        "hex": plaintext.hex().encode("ascii"),
    }


def _joined_text(values: tuple[str, ...]) -> bytes:
    return b"\0".join(value.encode("utf-8") for value in values)


def _runner_sinks(runner: RecordingRunner) -> Mapping[str, bytes]:
    arguments = tuple(
        argument
        for call_arguments, _environment in runner.calls
        for argument in call_arguments
    )
    environment = tuple(
        f"{name}={value}"
        for _arguments, call_environment in runner.calls
        for name, value in call_environment.items()
    )
    return {
        "runner argv": _joined_text(arguments),
        "runner environment": _joined_text(environment),
        "git stdout": b"".join(runner.standard_output_records),
        "git stderr": b"".join(runner.standard_error_records),
        "strace execve": b"".join(runner.child_process_records),
    }


def _sqlite_value_bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (int, float)):
        return str(value).encode("ascii")
    raise AssertionError(f"unexpected SQLite column type: {type(value).__name__}")


def _quoted_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_sinks(
    root: Path, required_rows: tuple[RequiredSqliteRow, ...]
) -> Mapping[str, bytes]:
    sinks: dict[str, bytes] = {}
    scanned_rows: dict[str, list[Mapping[str, object]]] = {}
    database_paths = tuple(sorted(root.rglob("*.sqlite")))
    assert database_paths, f"no SQLite databases found beneath {root}"
    for database_path in database_paths:
        database_name = database_path.relative_to(root).as_posix()
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            table_names = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            )
            connection.text_factory = bytes
            for table_name in table_names:
                cursor = connection.execute(
                    f"SELECT * FROM {_quoted_sqlite_identifier(table_name)}"
                )
                column_names = tuple(column[0] for column in cursor.description)
                rows = tuple(cursor)
                scanned_rows.setdefault(table_name, []).extend(
                    dict(zip(column_names, row, strict=True)) for row in rows
                )
                sinks[f"{database_name} table {table_name}"] = b"\0".join(
                    _sqlite_value_bytes(value) for row in rows for value in row
                )

            for suffix in ("", "-wal", "-shm"):
                storage_path = Path(f"{database_path}{suffix}")
                assert storage_path.is_file(), (
                    f"SQLite storage file was not available to scan: {storage_path}"
                )
                contents = storage_path.read_bytes()
                if not suffix:
                    assert contents, f"SQLite main file was empty: {storage_path}"
                sinks[f"{database_name}{suffix} file"] = contents

    for required_row in required_rows:
        assert required_row.table_name in scanned_rows, (
            f"required SQLite table was not scanned: {required_row.table_name}"
        )
        table_rows = scanned_rows[required_row.table_name]
        assert table_rows, (
            f"required SQLite table held no rows: {required_row.table_name}"
        )
        assert any(
            all(
                column_name in row
                and _sqlite_value_bytes(row[column_name])
                == _sqlite_value_bytes(expected_value)
                for column_name, expected_value in required_row.values.items()
            )
            for row in table_rows
        ), f"required SQLite row was not scanned from {required_row.table_name}"
    return sinks


def _log_sinks(
    formatted_logs: str, records: tuple[logging.LogRecord, ...]
) -> Mapping[str, bytes]:
    sinks = {"captured logs formatted": formatted_logs.encode("utf-8")}
    for index, record in enumerate(records):
        extra_attributes = {
            name: value
            for name, value in record.__dict__.items()
            if name not in _STANDARD_LOG_RECORD_ATTRIBUTES
        }
        fields = {
            "message": record.getMessage(),
            "args": record.args,
            "extra attributes": extra_attributes,
            "exception text": record.exc_text,
            "exception info": record.exc_info,
            "stack info": record.stack_info,
        }
        for field_name, value in fields.items():
            sinks[f"captured log {index} {field_name}"] = repr(value).encode(
                "utf-8", errors="backslashreplace"
            )
    return sinks


def _assert_canary_absent_from_sinks(
    encodings: Mapping[str, bytes], sinks: Mapping[str, bytes]
) -> None:
    for sink_name, contents in sinks.items():
        for encoding_name, encoded_canary in encodings.items():
            assert encoded_canary not in contents, (
                f"{encoding_name} canary leaked to {sink_name}"
            )


def test_token_canary_is_absent_from_durable_and_process_surfaces(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    canary = "p3-token+canary/never=copy"
    canary_encodings = _canary_encodings(canary)
    token_file = tmp_path / "token"
    token_file.write_text(canary, encoding="utf-8")
    project, remote, _base = _repositories(tmp_path)
    subprocess.run(
        ("git", "-C", str(remote), "config", "http.receivepack", "true"),
        check=True,
    )
    runner = RecordingRunner(tmp_path / "traces")
    runner.trace_directory.mkdir()
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )

    with GitHttpRemote(
        tmp_path,
        hashlib.sha256(canary_encodings["Authorization Basic value"]).digest(),
    ) as http_remote:
        push = GitTransportEffectAdapterFactory(
            tmp_path / CANDIDATE_STORE_DIRECTORY_NAME,
            GitRemote("http-canary-test", http_remote.url, token_file),
            AdapterRevision("git-push-v1"),
            EffectDestination("git"),
            FakeHeadBranchPullRequests(),
            runner,
        )
        registry = EffectAdapterRegistry(
            (
                EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
                EffectAdapterRegistration(
                    AdapterOperationName.PUSH_ATELIER_COMMIT, push
                ),
            )
        )
        executor = RecordingAgentExecutorFactoryV2(
            "exact",
            "exact/v1",
            "exact-operation",
            AGENT_OUTPUT,
            command=launching(
                sys.executable,
                "-c",
                _WRITE_CANDIDATE,
                b"candidate exact bytes\n".hex(),
                AGENT_OUTPUT.hex(),
            ),
        )
        runtime = DbosRuntime(
            DbosRuntimeSettings(
                tmp_path / "atelier.sqlite",
                "p3-canary-test",
                agent_scratch_root=agent_scratch_root(tmp_path),
                project_id=PROJECT,
                bootstrap_project_root=project,
                aco_executable=fake_agent_claim_executable(tmp_path),
            ),
            registry,
            (executor,),
        )
        runtime.initialize_storage()
        try:
            workflow, bindings = _publish(runtime)
            client = durable_api_client(
                runtime,
                served_project_id=PROJECT,
                tracker_item_source=FakeTrackerItemSource(
                    snapshot_answer=WorkItemRevisionObserved(
                        ObservedWorkItemRevision(
                            ITEM,
                            WorkItemKind.ISSUE,
                            b"Implement P3.\n\n## Dateien\n`one.txt`\n",
                            WorkItemChangeMarker("issue-642-canary"),
                            RecordedAt("2026-08-27T12:00:00Z"),
                        )
                    ),
                    expected_snapshot_reference=ITEM,
                ),
            )
            binding = bindings.bindings[0]
            response = client.post(
                API_PREFIX + "/runs",
                json={
                    "workflow_format_version": 3,
                    "run_id": RUN.value,
                    "workflow_revision_hash": workflow.revision_hash.value,
                    "agent_bindings": [
                        {
                            "role": binding.role.value,
                            "agent_configuration_revision_hash": (
                                binding.agent_configuration_revision_hash.value
                            ),
                        }
                    ],
                    "orders": [{"name": ORDER_NAME, "work_item": ITEM.value}],
                },
            )
            assert response.status_code == HTTPStatus.CREATED, response.text
            runtime.launch()
            bootstrap = DBOS.retrieve_workflow(bootstrap_workflow_id_for(RUN))
            assert bootstrap.get_result() == RunState.STARTED.value
            node = NodeExecutionId.for_node(RUN, workflow.revision_hash, "implement")
            node_workflow = DBOS.retrieve_workflow(node_workflow_id_for(node))
            assert node_workflow.get_result() == RunState.STARTED.value

            with runtime.engine.connect() as connection:
                intent = intent_snapshot_from_record(
                    connection.execute(
                        sa.select(effect_intents).where(
                            effect_intents.c.operation_name
                            == AdapterOperationName.PUSH_ATELIER_COMMIT.value
                        )
                    )
                    .mappings()
                    .one()
                ).intent
                receipt_rows = tuple(
                    connection.execute(sa.select(effect_receipts)).mappings()
                )
                event_rows = tuple(connection.execute(sa.select(run_events)).mappings())
            push_receipts = tuple(
                row
                for row in receipt_rows
                if row["operation_name"]
                == AdapterOperationName.PUSH_ATELIER_COMMIT.value
            )
            push_events = tuple(
                row
                for row in event_rows
                if row["receipt_logical_key"] == intent.binding.logical_key.value
                and row["event_kind"] == "ACTION_COMPLETED"
            )
            assert len(push_receipts) == 1
            assert len(push_events) == 1
            push_result = PushAtelierCommitReceipt.from_result_bytes(
                bytes(push_receipts[0]["result"])
            )
            assert (
                _git(remote, "rev-parse", push_result.full_ref)
                == push_receipts[0]["effect_id"]
            )
        finally:
            runtime.close()

    helper_process_records = tuple(
        line
        for record in runner.child_process_records
        for line in record.splitlines()
        if b'execve("/bin/sh"' in line and b"username=x-access-token" in line
    )
    assert helper_process_records
    sinks = {
        **_runner_sinks(runner),
        "credential helper execve": b"\n".join(helper_process_records),
        **_log_sinks(caplog.text, tuple(caplog.records)),
        **_sqlite_sinks(
            tmp_path,
            (
                RequiredSqliteRow(effect_receipts.name, dict(push_receipts[0])),
                RequiredSqliteRow(run_events.name, dict(push_events[0])),
            ),
        ),
    }
    _assert_canary_absent_from_sinks(canary_encodings, sinks)
