from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from dbos import DBOSClient

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    run_agent_bindings,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.grok_capability import (
    CONFORMANT_GROK_VERSIONS,
    verify_grok_capability,
)
from atelier2.adapters.grok_subscription import (
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_SUBSCRIPTION_FRAME_BYTES,
    GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    GROK_WORKSPACE_TOOLS_EXECUTOR_KEY,
    GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY,
    WORKSPACE_ALLOW_RULES,
    WORKSPACE_DENY_RULES,
    WORKSPACE_TOOLS,
    GrokContainmentUnattested,
    GrokExecutableUnsupported,
    GrokProviderEndedWithoutFinalMessage,
    GrokProviderEndedWithoutToolUse,
    GrokSubscriptionAuthModeUnsupported,
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionProcessCommand,
    GrokSubscriptionSettings,
    GrokWorkspaceToolExecutorFactory,
    attest_grok_workspace_tool_invocation,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.compose_node_job import NodeJobCompositionVersion, node_job
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    ProviderTerminalRefusal,
    ToolCalled,
    ToolReturned,
    UnrecognisedProviderOutput,
    Usage,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.orders import InlineOrderValue
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeReceipt,
    ProviderProbeResult,
    ProviderProbeVectorId,
)
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import (
    MAXIMUM_INSTANCE_DOCUMENT_BYTES,
    InstanceAccepted,
    InstanceRefused,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.when import recorded_instant
from atelier2.host import _grok_subscription_settings
from atelier2.host.serving import HostSettings, compose_application
from atelier2.ports.agent_attempts import AgentAttemptFailed, AgentAttemptSucceeded
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutionPreflightRefusal,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.durable_runs import (
    AuthoredOrder,
    DurableAgentExecutorCapabilityUnavailable,
    DurablePublishedRunResult,
    DurableRunCreated,
    StartPublishedRunRequestV2,
    StartPublishedRunRequestV3,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    declaring_mode_of,
    leased_directory_identity,
    publish_checked_model_registry,
    runtime_workspace_owner,
    workspace_files_nobody_opens,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA

MEASURED_GROK_VERSION = "1.0.5"


@pytest.fixture
def scratch_root_outside_a_worktree() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(
        prefix="atelier2-grok-scratch-", dir="/var/tmp"
    ) as directory:
        yield Path(directory)


HOST_DOCUMENT = f"""format_version: 3
name: Grok subscription host document
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: build
    outputs:
      - name: result
        schema: {{ref: result-schema, revision: {ANY_JSON_SCHEMA.revision_hash.value}}}
""".encode()
"""One node, and its own sink: the line's single real attempt is the whole run.

A second, trivially-completing node stood on the deleted V1/V2 grammar's
subworkflow kind; V3 has no node that completes without a real attempt, so a
second node here would need its own executor pass and, worse, would race this
file's cleanup assertion against a workspace that node had not yet vacated.
"""
GROK_PROBE_HEAD = """
import json, os, sys, tomllib
from pathlib import Path
if "--version" in sys.argv:
    print("grok 1.0.5 (5115b46bc9) [stable]")
    raise SystemExit(0)
if "inspect" in sys.argv:
    home = Path(os.environ["GROK_HOME"])
    compatibility = tomllib.loads((home / "config.toml").read_text())["compat"]
    cells = [
        {
            "vendor": vendor,
            "surface": surface,
            "enabled": compatibility[vendor].get(surface) is not False,
            "source": "config",
        }
        for vendor, surfaces in (
            ("cursor", ("skills", "rules", "agents", "mcps", "hooks", "sessions")),
            ("claude", ("skills", "rules", "agents", "mcps", "hooks", "sessions")),
            ("codex", ("sessions",)),
        )
        for surface in surfaces
    ]
    json.dump(
        {
            "plugins": [], "hooks": [], "mcpServers": [], "skills": [],
            "marketplaces": [], "lspServers": [], "projectInstructions": [],
            "configSources": {"layers": [{"role": "user", "path": str(home / "config.toml")}]},
            "permissions": {"sources": []},
            "agents": [{"name": "general-purpose", "source": {"type": "builtin"}}],
            "externalCompat": {"remoteSettingsLoaded": False, "cells": cells},
        },
        sys.stdout,
    )
    raise SystemExit(0)


def answer(payload, doors=()):
    '''Write this payload back in whichever output format the call asked for.

    The real CLI speaks two: one JSON envelope for the tool-free vector, and
    the Anthropic Messages NDJSON stream for the workspace-tool one, where the
    doors this fake really opened travel as their own messages.
    '''

    if "streaming-messages-json" in sys.argv:
        lines = [{"type": "system", "subtype": "init", "session_id": "fake-session"}]
        for index, (door, request, outcome) in enumerate(doors):
            call_id = "call-" + str(index)
            lines.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": call_id, "name": door, "input": request}
            ]}})
            lines.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": outcome}
            ]}})
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": json.dumps(payload)}
        ]}})
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(payload),
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
        lines.append(terminal)
        sys.stdout.write("\\n".join(json.dumps(line) for line in lines))
        return
    envelope = {"text": json.dumps(payload)}
    if "--json-schema" in sys.argv:
        envelope["structuredOutput"] = payload
    json.dump(envelope, sys.stdout)
"""
"""The version answer, the containment attestation, and the two output formats.

Both fakes below start here, because both stand in for the same CLI: what
separates them is only what they do where they are started.
"""

INTROSPECTING_GROK = (
    GROK_PROBE_HEAD
    + """
args = sys.argv[1:]
job = args[args.index("-p") + 1].encode("utf-8") if "-p" in args else b""
session = Path(os.environ["GROK_HOME"]) / "sessions" / "headless"
session.mkdir(parents=True)
written = session / "updates.jsonl"
written.write_bytes(job + b"\\nprovider response")
observed = {
    "arguments": sys.argv,
    "working_directory": os.getcwd(),
    "environment": dict(os.environ),
    "job": job.decode("utf-8"),
    "stdin": sys.stdin.buffer.read().decode("utf-8"),
}
answer(observed, [("search_replace", {"file_path": str(written)}, "written")])
"""
)

INLINE_PROMPT_GROK = INTROSPECTING_GROK.replace(
    '"arguments": sys.argv,',
    '"single_prompt_bytes": len(job),',
)


def _write_executable(path: Path, source: str) -> Path:
    # The resolved interpreter, because a fenced child follows this shebang
    # inside its own namespace, where a symlink chain out of the granted
    # interpreter tree leads nowhere.
    path.write_text(f"#!{Path(sys.executable).resolve()}\n" + source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def grok_named_deployment(
    root: Path, name: str, source: str
) -> GrokSubscriptionSettings:
    directory = root / name
    directory.mkdir()
    return grok_subscription_deployment(directory, source)


def _deployment_search_path() -> str:
    """The search path a deployment of these fake toolchains has to name.

    A fenced child reads and runs what its deployment's search path offers and
    nothing else, and these fakes are scripts rather than the real toolchain's
    one static executable: the interpreter tree they run on is part of what
    this deployment offers them, so it stands on the path beside this account's
    own.
    """

    return os.pathsep.join((os.environ.get("PATH", "/usr/bin"), sys.base_prefix))


def grok_subscription_deployment(
    tmp_path: Path, source: str
) -> GrokSubscriptionSettings:
    executable = _write_executable(tmp_path / "grok", source)
    workspace = tmp_path / "workspace"
    credentials = tmp_path / "grok-home"
    workspace.mkdir()
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    return GrokSubscriptionSettings(
        executable, workspace, credentials, _deployment_search_path()
    )


def subscription_request(
    model: str = "grok-4",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
    job: bytes = b"Reply with the single word pong",
    declared_output_schema: bytes | None = None,
    maximum_assistant_turns: int | None = None,
) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision("grok-primary", 1, ProviderId("xai"), auth_mode)
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("run-grok")
    revision_hash = WorkflowRevisionHash("3" * 64)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "build"),
        run_id,
        revision_hash,
        "build",
        ResolvedAgentBinding(AgentRole("builder"), configuration, auth),
        GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
        job,
        declared_output_schema,
        maximum_assistant_turns=maximum_assistant_turns,
    )


def leased_workspace(root: Path, name: str = "attempt-workspace") -> Path:
    """The blank directory an attempt's lease starts this provider in."""

    workspace = root / name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def leased(command: AgentProcessCommand, workspace: Path) -> AgentProcessInvocation:
    """One invocation for a test that houses the leased directory itself."""

    return AgentProcessInvocation(
        command,
        leased_directory_identity(
            agent_attempt_execution(subscription_request()).attempt_id, workspace
        ),
    )


def measured_headless_json_envelope(
    *,
    text: str,
    thought: str,
    structured_output: object | None = None,
) -> bytes:
    """One grok 1.0.4 `--output-format json` completion, local scenario values.

    `structured_output` is scenario data for the `structuredOutput` field the
    CLI adds when `--json-schema` is set; schema-bearing commands serialize it
    for the output seam.
    """

    envelope: dict[str, object] = {
        "text": text,
        "thought": thought,
        "stopReason": "end_turn",
        "sessionId": "00000000-0000-4000-8000-000000000001",
        "requestId": "00000000-0000-4000-8000-000000000002",
        "usage": {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "total_tokens": 2,
        },
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "total_cost_usd_ticks": 0,
        "modelUsage": {},
    }
    if structured_output is not None:
        envelope["structuredOutput"] = structured_output
    return json.dumps(envelope).encode()


def recorded_grok_json_values(*values: object) -> bytes:
    """The JSON values grok 1.0.4 concatenated without a record separator."""

    return b"".join(json.dumps(value, ensure_ascii=False).encode() for value in values)


def launched(command: AgentProcessCommand, workspace: Path) -> AgentProcessCompletion:
    """Run one prepared command exactly where the attempt leased its ground."""

    completed = subprocess.run(
        command.arguments,
        cwd=workspace,
        env=dict(command.environment),
        input=command.standard_input,
        capture_output=True,
        check=False,
    )
    return AgentProcessCompletion(
        completed.returncode, completed.stdout, completed.stderr
    )


def test_a_headless_run_carries_the_bound_model_job_and_only_the_credential_boundary(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    # Distinctive spacing so a json.loads/dumps rewrite of the published
    # document fails this pin rather than silently agreeing.
    declared_schema = b'{"type": "object", "additionalProperties": false}'
    request = subscription_request(
        model="grok-4", job=b"draw the owl", declared_output_schema=declared_schema
    )

    workspace = leased_workspace(tmp_path)
    command = executor.prepare_process(request)
    invocation = leased(command, workspace)
    assert command.arguments == (
        str(settings.executable),
        "--output-format",
        "json",
        "--json-schema",
        '{"type": "object", "additionalProperties": false}',
        "--model",
        "grok-4",
        "-p",
        "draw the owl",
        "--tools=",
        "--permission-mode",
        "dontAsk",
        "--no-memory",
        "--no-subagents",
        "--disable-web-search",
        "--max-turns",
        "16",
    )
    invocation_home = Path(dict(command.environment)["GROK_HOME"])
    # The secret channel keeps its own directory; the ground the provider stands
    # on is the attempt's lease, and the two are deliberately not one place.
    assert invocation.lease.working_directory == workspace
    assert invocation_home != workspace
    assert invocation_home != settings.credential_directory
    assert invocation_home.parent == settings.workspace
    assert command.environment == (
        # HOME is named on purpose: measured on grok 1.0.4, a child without it
        # resolves the invoking account's home and loads that profile's
        # plugins, hooks and skills.
        ("HOME", str(invocation_home)),
        ("GROK_HOME", str(invocation_home)),
        ("PATH", settings.search_path),
        # SHELL and TERM are named on purpose too: measured on grok 1.0.5, a
        # `run_terminal_cmd` that stays silent for ~25s cancels the session
        # under a child environment without them (issue #642).
        ("SHELL", "/bin/bash"),
        ("TERM", "xterm-256color"),
    )
    assert command.standard_input == b""
    assert command.standard_output_frame_bytes == GROK_SUBSCRIPTION_FRAME_BYTES
    assert b"draw the owl" in " ".join(command.arguments).encode()
    result = executor.decode_process_completion(
        invocation, launched(command, workspace)
    )
    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][0] == str(settings.executable)
    assert (
        observed["arguments"][observed["arguments"].index("--json-schema") + 1]
        == '{"type": "object", "additionalProperties": false}'
    )
    assert observed["job"] == "draw the owl"
    assert observed["stdin"] == ""
    assert observed["environment"]["GROK_HOME"] == str(invocation_home)
    assert observed["environment"]["PATH"] == settings.search_path
    assert observed["environment"]["HOME"] == str(invocation_home)
    assert observed["environment"]["SHELL"] == "/bin/bash"
    assert observed["environment"]["TERM"] == "xterm-256color"
    assert "XAI_API_KEY" not in observed["environment"]
    assert (invocation_home / "sessions" / "headless" / "updates.jsonl").exists()
    executor.release_credential_channel(command)
    assert not invocation_home.exists()
    assert (settings.credential_directory / "auth.json").read_bytes() == b"{}"


@pytest.mark.parametrize(
    "factory",
    (GrokSubscriptionExecutorFactory, GrokWorkspaceToolExecutorFactory),
)
def test_a_measured_size_job_reaches_grok_inline(
    tmp_path: Path,
    factory: type[GrokSubscriptionExecutorFactory | GrokWorkspaceToolExecutorFactory],
) -> None:
    settings = grok_subscription_deployment(tmp_path, INLINE_PROMPT_GROK)
    executor = factory(settings).open()
    job = b"x" * 30_000
    command = executor.prepare_process(subscription_request(job=job))
    invocation = leased(command, leased_workspace(tmp_path))

    completion = launched(command, invocation.lease.working_directory)
    result = executor.decode_process_completion(invocation, completion)

    assert completion.return_code == 0
    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["single_prompt_bytes"] == len(job)
    assert observed["stdin"] == ""
    executor.release_credential_channel(command)


def test_a_job_above_the_measured_transport_limit_is_agent_refused(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(
        tmp_path, "raise AssertionError('a Grok process was launched')\n"
    )
    executor = GrokSubscriptionExecutorFactory(settings).open()
    job = b"x" * 30_001

    with pytest.raises(AgentExecutionPreflightRefusal, match="30,000") as refused:
        executor.prepare_process(subscription_request(job=job))

    assert refused.value.code is AgentAttemptFailureCode.AGENT_REFUSED
    assert list(settings.workspace.iterdir()) == []


@pytest.mark.parametrize(
    "factory",
    (GrokSubscriptionExecutorFactory, GrokWorkspaceToolExecutorFactory),
)
def test_non_utf8_job_bytes_are_agent_refused_before_any_grok_invocation(
    tmp_path: Path,
    factory: type[GrokSubscriptionExecutorFactory | GrokWorkspaceToolExecutorFactory],
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = factory(settings).open()

    with pytest.raises(AgentExecutionPreflightRefusal, match="UTF-8") as refused:
        executor.prepare_process(subscription_request(job=b"\xff"))

    assert refused.value.code is AgentAttemptFailureCode.AGENT_REFUSED
    assert list(settings.workspace.iterdir()) == []


def test_a_string_schema_travels_as_json_schema(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(
        declared_output_schema=b'{"type": "string", "minLength": 1}'
    )

    command = executor.prepare_process(request)

    assert command.arguments[command.arguments.index("--json-schema") + 1] == (
        '{"type": "string", "minLength": 1}'
    )
    executor.release_credential_channel(command)


@pytest.mark.parametrize(
    ("prose", "expected_verdict"),
    (
        (
            "Befund 1: der Diff nennt die apt-Lock-Heilung.\nVerdict: revise",
            InstanceAccepted,
        ),
        ("Befund 1: der Diff nennt die apt-Lock-Heilung.", InstanceRefused),
    ),
)
def test_a_string_schema_answer_is_judged_by_its_declared_schema(
    tmp_path: Path,
    prose: str,
    expected_verdict: type[InstanceAccepted | InstanceRefused],
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    declared_schema = (
        b'{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        b'"type":"string",'
        b'"pattern":"^Befund 1[^0-9][\\\\s\\\\S]*\\\\nVerdict: '
        b'(accepted|revise)$"}'
    )
    request = subscription_request(declared_output_schema=declared_schema)
    command = executor.prepare_process(request)
    invocation = leased(command, leased_workspace(tmp_path))
    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=prose,
                thought="Ich prüfe zuerst die Regeln.",
                structured_output=prose,
            ),
            b"",
        ),
    )

    assert isinstance(result, AgentExecutionResult)
    assert command.arguments[command.arguments.index("--json-schema") + 1] == (
        declared_schema.decode()
    )
    assert json.loads(result.output_bytes) == prose
    schema = read_schema_document(declared_schema)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(
        read_instance_document(result.output_bytes, schema), expected_verdict
    )
    executor.release_credential_channel(command)


@pytest.mark.parametrize(
    "prose",
    (
        "Befund 1: der Diff nennt die apt-Lock-Heilung.\nVerdict: revise",
        'Befund: "C:\\\\atelier\\\\report.json"',
    ),
)
def test_a_string_schema_answer_uses_the_provider_structured_value(
    tmp_path: Path,
    prose: str,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(declared_output_schema=b'{"type":"string"}')
    command = executor.prepare_process(request)
    invocation = leased(command, leased_workspace(tmp_path))
    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=prose,
                thought="Ich prüfe zuerst die Regeln.",
                structured_output=prose,
            ),
            b"",
        ),
    )

    assert isinstance(result, AgentExecutionResult)
    assert "--json-schema" in command.arguments
    assert json.loads(result.output_bytes) == prose
    executor.release_credential_channel(command)


def test_a_schema_bearing_command_carries_the_declared_schema(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    command = executor.prepare_process(
        subscription_request(declared_output_schema=b'{"type":"string"}')
    )

    assert isinstance(command, GrokSubscriptionProcessCommand)
    assert command.declared_output_schema_bytes == b'{"type":"string"}'
    assert "--json-schema" in command.arguments
    executor.release_credential_channel(command)


def test_schema_output_serialization_reads_the_command_not_home(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    prose = "Befund 1: der Diff nennt die apt-Lock-Heilung."
    command = GrokSubscriptionProcessCommand(
        ("grok",),
        (),
        standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        declared_output_schema_bytes=b'{"type":"string"}',
    )

    result = executor.decode_process_completion(
        leased(command, leased_workspace(tmp_path)),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=prose,
                thought="Ich prüfe zuerst die Regeln.",
                structured_output=prose,
            ),
            b"",
        ),
    )

    assert isinstance(result, AgentExecutionResult)
    assert result == AgentExecutionResult(
        json.dumps(prose, ensure_ascii=False).encode()
    )


def test_an_unmarked_command_does_not_quote_free_text(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    prose = "Befund 1: der Diff nennt die apt-Lock-Heilung."
    command = GrokSubscriptionProcessCommand(
        ("grok",),
        (),
        standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
    )

    result = executor.decode_process_completion(
        leased(command, leased_workspace(tmp_path)),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=prose,
                thought="Ich prüfe zuerst die Regeln.",
            ),
            b"",
        ),
    )

    assert isinstance(result, AgentExecutionResult)
    assert result == AgentExecutionResult(prose.encode())


def test_an_object_schema_travels_as_exact_json_schema_bytes(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    command = executor.prepare_process(
        subscription_request(
            declared_output_schema=b'{"type": "object", "additionalProperties": false}'
        )
    )

    assert isinstance(command, GrokSubscriptionProcessCommand)
    assert command.declared_output_schema_bytes == (
        b'{"type": "object", "additionalProperties": false}'
    )
    assert command.arguments[command.arguments.index("--json-schema") + 1] == (
        '{"type": "object", "additionalProperties": false}'
    )
    executor.release_credential_channel(command)


def test_a_node_without_a_declared_schema_does_not_invent_a_json_schema_flag(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    command = executor.prepare_process(subscription_request())

    assert "--json-schema" not in command.arguments
    executor.release_credential_channel(command)


def test_a_non_subscription_profile_is_refused_before_any_invocation(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(auth_mode=AuthMode.API_KEY)

    with pytest.raises(GrokSubscriptionAuthModeUnsupported):
        executor.prepare_process(request)


def test_concatenated_grok_progress_values_stay_in_the_transcript(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )
    first_finding = f"{'Befund'} {1}"
    progress = (
        (
            f'"{first_finding}" is the required first token; I am gathering '
            "the surrounding contract, callers, and tests."
        ),
        (
            f'"{first_finding}" stays first in the final reply; I am comparing '
            "the empty-file gate with its host-init twin."
        ),
    )
    answer = '{"verdict":"pass"}'
    final_envelope = json.loads(
        measured_headless_json_envelope(
            text=answer,
            thought="I have enough evidence to answer now.",
        )
    )

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            recorded_grok_json_values(*progress, final_envelope),
            b"",
        ),
    )

    assert result == AgentExecutionResult(
        answer.encode(),
        AttemptTranscript.of([AssistantTurn(message) for message in progress]),
    )


@pytest.mark.parametrize(
    "standard_output",
    [
        pytest.param(
            b'{"text":"complete"}{"text":',
            id="a partial trailing JSON value",
        ),
        pytest.param(
            b"1" + (b"0" * 4_300),
            id="an integer beyond the JSON decoder limit",
        ),
        pytest.param(
            b'{"text":"complete"}\xff',
            id="invalid UTF-8 after a JSON value",
        ),
    ],
)
def test_unreadable_concatenated_grok_values_are_typed_failures(
    tmp_path: Path, standard_output: bytes
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )

    result = executor.decode_process_completion(
        invocation, AgentProcessCompletion(0, standard_output, b"")
    )

    assert isinstance(result, AgentExecutionFailure)
    assert result.code == AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
    assert result.transcript is not None


@pytest.mark.parametrize(
    ("standard_output", "expected"),
    [
        pytest.param(
            recorded_grok_json_values(7, {"text": '{"verdict":"pass"}'}),
            AgentExecutionResult(
                b'{"verdict":"pass"}',
                AttemptTranscript.of([UnrecognisedProviderOutput("7")]),
            ),
            id="a scalar before the envelope",
        ),
        pytest.param(
            recorded_grok_json_values(
                ["collecting evidence"], {"text": '{"verdict":"pass"}'}
            ),
            AgentExecutionResult(
                b'{"verdict":"pass"}',
                AttemptTranscript.of(
                    [UnrecognisedProviderOutput('["collecting evidence"]')]
                ),
            ),
            id="an array before the envelope",
        ),
        pytest.param(
            recorded_grok_json_values(
                {"text": '{"verdict":"pass"}'}, "still collecting evidence"
            ),
            GrokProviderEndedWithoutFinalMessage(
                AttemptTranscript.of(
                    [
                        UnrecognisedProviderOutput(
                            '{"text":"{\\"verdict\\":\\"pass\\"}"}'
                        ),
                        AssistantTurn("still collecting evidence"),
                    ]
                )
            ),
            id="progress after an envelope means the last value wins",
        ),
    ],
)
def test_concatenated_grok_values_use_only_the_last_value_as_the_envelope(
    tmp_path: Path,
    standard_output: bytes,
    expected: AgentExecutionResult | GrokProviderEndedWithoutFinalMessage,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )

    assert (
        executor.decode_process_completion(
            invocation, AgentProcessCompletion(0, standard_output, b"")
        )
        == expected
    )


def test_grok_ending_after_concatenated_progress_has_no_final_message(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        GrokSubscriptionProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
            declared_output_schema_bytes=b'{"type":"string"}',
        ),
        tmp_path,
    )
    progress = (
        (
            "Befund 1 is the required first token; I will inspect the "
            "surrounding owner, callers, and tests for "
            "`_project_source_connection` before writing it."
        ),
        (
            "Befund 1 still has to name a real defect; next I'll check how "
            "empty stores are treated at the connect command and in tests."
        ),
    )
    recorded_output = (
        b'"Befund 1 is the required first token; I will inspect the '
        b"surrounding owner, callers, and tests for "
        b'`_project_source_connection` before writing it."'
        b"\"Befund 1 still has to name a real defect; next I'll check how "
        b'empty stores are treated at the connect command and in tests."'
    )

    assert len(recorded_output) == 273
    assert recorded_output == recorded_grok_json_values(*progress)

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(0, recorded_output, b""),
    )

    assert result == GrokProviderEndedWithoutFinalMessage(
        AttemptTranscript.of([AssistantTurn(message) for message in progress])
    )


def test_the_final_answer_reaches_the_output_seam_not_the_turn_narration(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )
    narration = "Ich prüfe zuerst die Dateien und die Werkzeuge."
    answer = '{"verdict":"pass"}'

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(text=answer, thought=narration),
            b"",
        ),
    )

    assert result == AgentExecutionResult(answer.encode())


def test_a_schema_bearing_envelope_yields_the_provider_structured_value(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        GrokSubscriptionProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
            declared_output_schema_bytes=b'{"type":"string"}',
        ),
        tmp_path,
    )
    narration = "Ich prüfe zuerst die Dateien und die Werkzeuge."
    answer = '"wrong-token"'

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=answer,
                thought=narration,
                structured_output="pass-token",
            ),
            b"",
        ),
    )

    assert result == AgentExecutionResult(b'"pass-token"')
    assert result != AgentExecutionResult(answer.encode())
    assert result != AgentExecutionResult(narration.encode())


def test_a_null_structured_output_is_an_ended_provider_not_a_null_answer(
    tmp_path: Path,
) -> None:
    """`"structuredOutput": null` is grok's no-answer sentinel, never an answer.

    The envelope is a recorded grok 1.0.5 completion of the workspace-tool
    vector whose model ended without structured output; live run 91c76c25
    published the same shape with exit code 0, where decoding the null as a
    JSON answer handed the output seam a fabricated `b"null"` and dropped
    the envelope from the evidence.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        GrokSubscriptionProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
            declared_output_schema_bytes=b'{"type":"string"}',
        ),
        tmp_path,
    )
    recorded_envelope = rb"""{
  "text": "\"I'll create `y.txt` with the word alpha first, then reply with the JSON string after that write finishes.\"",
  "stopReason": "cancelled",
  "sessionId": "01a058a0-068c-7a02-bb6f-cc93c47af5f5",
  "requestId": "363041fa-e87d-4058-8e69-f70614590dd3",
  "thought": "The user wants me to:\n1. First create a file named y.txt containing the word alpha using a tool",
  "usage": {
    "input_tokens": 6831,
    "cache_read_input_tokens": 128,
    "cache_creation_input_tokens": 0,
    "output_tokens": 265,
    "reasoning_tokens": 211,
    "total_tokens": 7224
  },
  "num_turns": 1,
  "total_cost_usd": 0.00260372,
  "total_cost_usd_ticks": 26037200,
  "modelUsage": {
    "grok-4.6-build": {
      "inputTokens": 6831,
      "outputTokens": 265,
      "cacheReadInputTokens": 128,
      "cacheCreationInputTokens": 0,
      "modelCalls": 1,
      "costUSD": 0.00260372
    }
  },
  "structuredOutput": null,
  "structuredOutputError": "model did not produce structured output"
}
"""

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(0, recorded_envelope, b""),
    )

    assert isinstance(result, GrokProviderEndedWithoutFinalMessage)
    assert result.transcript is not None
    (envelope_evidence,) = result.transcript.events
    assert isinstance(envelope_evidence, UnrecognisedProviderOutput)
    assert "model did not produce structured output" in envelope_evidence.text


def test_a_normal_grok_answer_that_mentions_offloaded_files_is_not_refused(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        GrokSubscriptionProcessCommand(
            ("grok", "-p", "build"),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text="I offloaded the draft to a file for review.",
                thought="I considered an offloaded file while preparing the answer.",
            ),
            b"",
        ),
    )

    assert result == AgentExecutionResult(
        b"I offloaded the draft to a file for review."
    )


def test_an_unusable_envelope_is_a_typed_process_failure(tmp_path: Path) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )
    refusal = AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
    )
    narration = "Ich prüfe zuerst die Dateien und die Werkzeuge."

    failed_with_output = executor.decode_process_completion(
        invocation, AgentProcessCompletion(1, b'{"text":"no"}', b"")
    )
    assert isinstance(failed_with_output, AgentExecutionFailure)
    assert failed_with_output.code == refusal.code
    assert failed_with_output.transcript is not None
    not_json = executor.decode_process_completion(
        invocation, AgentProcessCompletion(0, b"not-json", b"")
    )
    assert isinstance(not_json, AgentExecutionFailure)
    assert not_json.code == refusal.code
    wrong_field = executor.decode_process_completion(
        invocation, AgentProcessCompletion(0, b'{"result":"wrong field"}', b"")
    )
    assert isinstance(wrong_field, AgentExecutionFailure)
    assert wrong_field.code == refusal.code
    assert (
        executor.decode_process_completion(
            invocation, AgentProcessCompletion(0, b"", b"")
        )
        == GrokProviderEndedWithoutFinalMessage()
    )
    for completion in (
        AgentProcessCompletion(0, b"{}", b""),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(text="", thought=narration),
            b"",
        ),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text="",
                thought=narration,
                structured_output="pass-token",
            ),
            b"",
        ),
    ):
        result = executor.decode_process_completion(invocation, completion)
        assert isinstance(result, GrokProviderEndedWithoutFinalMessage)


def test_only_the_measured_grok_release_is_admitted(tmp_path: Path) -> None:
    assert CONFORMANT_GROK_VERSIONS == {(1, 0, 5)}
    installed = grok_named_deployment(
        tmp_path,
        "installed",
        INTROSPECTING_GROK,
    )
    assert verify_grok_capability(installed.executable) == (1, 0, 5)
    other = grok_named_deployment(
        tmp_path,
        "other",
        "import sys\nprint('grok 1.0.4 (old) [stable]')\n",
    )
    with pytest.raises(GrokExecutableUnsupported, match="1.0.5"):
        verify_grok_capability(other.executable)


ATTESTING_GROK = """
import json, os, sys
from pathlib import Path
if "--version" in sys.argv:
    print("grok 1.0.5 (5115b46bc9) [stable]")
    raise SystemExit(0)
if "inspect" in sys.argv:
    inspected = dict(INSPECTED)
    if inspected.get("configSources") == "exact-job-config":
        inspected["configSources"] = {
            "layers": [
                {
                    "role": "user",
                    "path": str(Path(os.environ["GROK_HOME"]) / "config.toml"),
                }
            ]
        }
    print(json.dumps(inspected))
    raise SystemExit(0)
raise SystemExit(3)
"""

INERT_PROFILE = {
    "grokVersion": MEASURED_GROK_VERSION,
    "plugins": [],
    "hooks": [],
    "mcpServers": [],
    "skills": [],
    "marketplaces": [],
    "lspServers": [],
    "projectInstructions": [],
    "configSources": "exact-job-config",
    "permissions": {"sources": []},
    "agents": [{"name": "general-purpose", "source": {"type": "builtin"}}],
    "externalCompat": {
        "remoteSettingsLoaded": False,
        "cells": [
            {
                "vendor": vendor,
                "surface": surface,
                "enabled": False,
                "source": "config",
            }
            for vendor, surfaces in (
                (
                    "cursor",
                    ("skills", "rules", "agents", "mcps", "hooks", "sessions"),
                ),
                (
                    "claude",
                    ("skills", "rules", "agents", "mcps", "hooks", "sessions"),
                ),
                ("codex", ("sessions",)),
            )
            for surface in surfaces
        ],
    },
}


def attesting_deployment(
    tmp_path: Path, profile: dict[str, object]
) -> GrokSubscriptionSettings:
    source = f"INSPECTED = {profile!r}\n" + ATTESTING_GROK
    return grok_subscription_deployment(tmp_path, source)


def test_the_private_credential_home_outlives_neither_a_completion_nor_a_close(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    invocation = executor.prepare_process(subscription_request())
    invocation_home = Path(dict(invocation.environment)["GROK_HOME"])
    assert invocation_home.exists()

    executor.decode_process_completion(
        leased(invocation, tmp_path), AgentProcessCompletion(1, b"", b"")
    )
    executor.release_credential_channel(invocation)

    assert not invocation_home.exists()

    second = executor.prepare_process(subscription_request())
    abandoned_home = Path(dict(second.environment)["GROK_HOME"])
    assert abandoned_home.exists()

    executor.close()

    assert not abandoned_home.exists()
    assert list(settings.workspace.iterdir()) == []


def test_releasing_one_concurrent_invocation_preserves_the_other(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    first = executor.prepare_process(subscription_request(job=b"first"))
    second = executor.prepare_process(subscription_request(job=b"second"))
    first_home = Path(dict(first.environment)["GROK_HOME"])
    second_home = Path(dict(second.environment)["GROK_HOME"])

    executor.release_credential_channel(first)

    assert not first_home.exists()
    assert second_home.is_dir()
    assert second.arguments[second.arguments.index("-p") + 1] == "second"
    executor.release_credential_channel(second)
    assert list(settings.workspace.iterdir()) == []


def test_any_enabled_external_compatibility_cell_refuses_the_exact_launch(
    tmp_path: Path,
) -> None:
    profile = dict(INERT_PROFILE)
    compatibility = dict(INERT_PROFILE["externalCompat"])
    cells = [dict(cell) for cell in compatibility["cells"]]
    cells[0]["enabled"] = True
    compatibility["cells"] = cells
    profile["externalCompat"] = compatibility
    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match="externalCompat"):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_real_host_runtime_supervisor_executes_and_cleans_without_a_billed_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_root_outside_a_worktree: Path,
) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    candidate = grok_subscription_deployment(deployment, INTROSPECTING_GROK)
    monkeypatch.setenv("PATH", candidate.search_path)
    parser = argparse.ArgumentParser()
    declared = _grok_subscription_settings(
        parser,
        argparse.Namespace(
            grok_executable=candidate.executable,
            grok_workspace=candidate.workspace,
            grok_credential_directory=candidate.credential_directory,
            grok_workspace_tools=False,
        ),
    )
    assert declared.settings == candidate
    assert declared.start_refusal is None

    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index", encoding="utf-8")
    common = {
        "database_path": tmp_path / "durable.sqlite",
        "effect_store_path": tmp_path / "effects.sqlite",
        "effect_adapter_revision": "loopback-v1",
        "effect_destination": "grok-host-proof",
        "application_version": "grok-host-proof",
        "source_commit": "c" * 40,
        "source_tree": "proof",
        "frontend_dist": frontend,
        # Isolated per test, never the operator's real XDG state directory:
        # a stray real receipt must never make an unrelated test's gate
        # answer depend on what happens to sit on the machine running it.
        "provider_probe_receipt_directory": tmp_path / "provider-probes",
        "agent_scratch_root": scratch_root_outside_a_worktree,
        "grok_subscription": declared.settings,
    }
    with pytest.raises(ValueError, match="loopback"):
        HostSettings(**common, host="0.0.0.0")

    settings = HostSettings(**common)
    _app, runtime = compose_application(settings)
    runtime.initialize_storage()
    try:
        assert runtime.agent_executor_registry.keys == frozenset(
            {GROK_SUBSCRIPTION_EXECUTOR_KEY}
        )
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        auth = AuthProfileRevision(
            "grok-host", 1, ProviderId("xai"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        interactive = AgentConfigurationRevision(
            # A distinct model name from `headless` below: the checked model
            # registry names one canonical configuration per model, so the two
            # capability variants under study here need two names to both be
            # registered and reachable by their own exact binding.
            "grok-4-interactive",
            auth.revision_hash,
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.INTERACTIVE,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        headless = AgentConfigurationRevision(
            "grok-4",
            auth.revision_hash,
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(interactive),
            AgentConfigurationRevisionCreated,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(headless),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId("xai"), (interactive, headless)
        )
        DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
        workflow = WorkflowRevision(HOST_DOCUMENT)
        DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
        starter = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
        )
        refused = starter.start_published(
            StartPublishedRunRequestV2(
                RunId("grok/interactive-refused"),
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), interactive.revision_hash),)
                ),
            )
        )
        assert isinstance(refused, DurableAgentExecutorCapabilityUnavailable)
        assert settings.provider_probe_receipt_directory is not None
        settings.provider_probe_receipt_directory.mkdir(parents=True, exist_ok=True)
        assert runtime.settings.provider_probe_receipt_provider_layer_digest is not None
        now = datetime.now(UTC)
        headless_receipt = ProviderProbeReceipt(
            ProviderProbeVectorId("headless-grok-4"),
            headless.revision_hash,
            WorkflowRevisionHash("b" * 64),
            runtime.settings.provider_probe_receipt_provider_layer_digest,
            settings.source_commit,
            recorded_instant(now - timedelta(minutes=1)),
            recorded_instant(now + timedelta(hours=1)),
            ProviderProbeResult.SUCCEEDED,
            RunId("provider-canary/grok-4-fixture"),
            terminal_hash=Sha256Hash("d" * 64),
        )
        (settings.provider_probe_receipt_directory / "grok-4.json").write_bytes(
            headless_receipt.canonical_bytes()
        )
        started = starter.start_published(
            StartPublishedRunRequestV2(
                RunId("grok/headless"),
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), headless.revision_hash),)
                ),
            )
        )
        assert isinstance(started, DurableRunCreated)
        runtime.launch()
        deadline = time.monotonic() + 10
        state = ""
        workspace_cleared = False
        while time.monotonic() < deadline:
            with runtime.engine.connect() as connection:
                state = str(
                    connection.scalar(
                        runs.select()
                        .with_only_columns(runs.c.state)
                        .where(runs.c.run_id == "grok/headless")
                    )
                )
            workspace_cleared = not list(candidate.workspace.iterdir())
            if state == "COMPLETED" and workspace_cleared:
                break
            time.sleep(0.02)
        assert state == "COMPLETED"
        assert workspace_cleared
        assert (candidate.credential_directory / "auth.json").read_bytes() == b"{}"
    finally:
        runtime.close()


def test_a_headless_run_leaves_memory_subagents_and_web_search_disabled(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    invocation = executor.prepare_process(subscription_request())

    assert "--no-memory" in invocation.arguments
    assert "--no-subagents" in invocation.arguments
    assert "--disable-web-search" in invocation.arguments
    executor.close()


def test_an_inert_profile_is_attested(tmp_path: Path) -> None:
    settings = attesting_deployment(tmp_path, INERT_PROFILE)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    invocation = executor.prepare_process(subscription_request())

    executor.release_credential_channel(invocation)


@pytest.mark.parametrize(
    ("surface", "discovered"),
    [
        ("plugins", [{"name": "marketplace-plugin"}]),
        ("hooks", [{"event": "PreToolUse"}]),
        ("mcpServers", [{"name": "filesystem"}]),
        ("skills", [{"name": "deploy"}]),
        ("marketplaces", [{"name": "community"}]),
        ("lspServers", [{"name": "pyright"}]),
        ("projectInstructions", [{"path": "AGENTS.md"}]),
        ("configSources", [{"path": "/home/operator/.grok/config.toml"}]),
    ],
)
def test_a_discovered_trust_surface_refuses_to_serve(
    tmp_path: Path, surface: str, discovered: list[object]
) -> None:
    profile = dict(INERT_PROFILE)
    profile[surface] = discovered

    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match=surface):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_a_discovered_permission_source_refuses_to_serve(tmp_path: Path) -> None:
    profile = dict(INERT_PROFILE)
    profile["permissions"] = {"sources": ["/etc/claude-code/managed-settings.json"]}

    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match="permissions.sources"):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_a_non_builtin_agent_refuses_to_serve(tmp_path: Path) -> None:
    profile = dict(INERT_PROFILE)
    profile["agents"] = [{"name": "deployer", "source": {"type": "project"}}]

    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match="agents"):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_an_unreadable_attestation_refuses_rather_than_assuming_containment(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(
        tmp_path, "import sys\nsys.exit(0 if '--version' in sys.argv else 9)\n"
    )

    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_the_credential_channel_dies_while_the_leased_workspace_stands(
    tmp_path: Path,
) -> None:
    """Two owners, two disciplines, and the secret's is the shorter one.

    The attempt's workspace keeps what a provider left behind until the attempt
    is durably terminal, because that is evidence. The private home holding a
    copy of the operator's `auth.json` is not evidence, and it falls on every
    path -- after a decoded answer and after a refused one alike -- while the
    workspace it ran in is untouched.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    workspace = leased_workspace(tmp_path)

    for outcome in (AgentProcessCompletion(0, b'{"text":"ok"}', b""), None):
        command = executor.prepare_process(subscription_request())
        channel = Path(dict(command.environment)["GROK_HOME"])
        (workspace / "provider-left-this").write_text("kept", encoding="utf-8")
        assert channel.is_dir()
        assert (channel / "auth.json").exists()

        if outcome is not None:
            executor.decode_process_completion(leased(command, workspace), outcome)
        executor.release_credential_channel(command)

        assert not channel.exists()
        assert (workspace / "provider-left-this").read_text() == "kept"
    executor.close()


WORKSPACE_ARTEFACT_NAME = "artefact"
WORKSPACE_ARTEFACT_LINE = "from the lease"

TOOL_USING_GROK = (
    GROK_PROBE_HEAD
    + f"""
written = os.path.join(os.getcwd(), {WORKSPACE_ARTEFACT_NAME!r})
with open(written, "w", encoding="utf-8") as artefact:
    artefact.write({WORKSPACE_ARTEFACT_LINE!r})
with open(written, encoding="utf-8") as artefact:
    read_back = artefact.read()
answer(
    {{"wrote": written, "read_back": read_back}},
    [
        ("search_replace", {{"file_path": written}}, "written"),
        ("read_file", {{"target_file": written}}, read_back),
    ],
)
"""
)
"""A fake that really writes and reads back where it was started."""


def grok_subscription_runtime(
    root: Path,
    settings: GrokSubscriptionSettings,
    scratch_root: Path,
    *,
    workspace_tools: bool = False,
) -> DbosRuntime:
    """The production runtime, serving the Grok executors this scenario arms."""

    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "grok-subscription-test",
            agent_scratch_root=scratch_root,
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("grok-subscription-test"),
        ),
        (
            GrokSubscriptionExecutorFactory(settings),
            *((GrokWorkspaceToolExecutorFactory(settings),) if workspace_tools else ()),
        ),
    )


def grok_subscription_start(
    runtime: DbosRuntime,
    run_name: str,
    requested_capability: AgentExecutionCapability,
    executor_revision: AgentExecutorRevision,
    job: bytes = b"build",
) -> tuple[DurablePublishedRunResult, WorkflowRevision]:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision(
        "grok-tools", 1, ProviderId("xai"), AuthMode.SUBSCRIPTION
    )
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        "grok-4",
        auth.revision_hash,
        executor_revision,
        requested_capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog.publish_agent_configuration_revision(configuration)
    publish_checked_model_registry(runtime.engine, ProviderId("xai"), (configuration,))
    DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    workflow = WorkflowRevision(
        declaring_mode_of(
            HOST_DOCUMENT.replace(b"instruction: build", b"instruction: " + job),
            requested_capability,
        )
    )
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(
            RunId(run_name),
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
            ),
        )
    )
    return started, workflow


def grok_subscription_attempt(
    runtime: DbosRuntime,
    run_name: str,
    requested_capability: AgentExecutionCapability,
    executor_revision: AgentExecutorRevision,
    operational_identity: AgentExecutorOperationalIdentity,
    job: bytes = b"build",
) -> AgentAttemptExecution:
    run_id = RunId(run_name)
    started, workflow = grok_subscription_start(
        runtime, run_name, requested_capability, executor_revision, job
    )
    assert isinstance(started, DurableRunCreated)
    assert isinstance(started.run, RunV3)
    return agent_attempt_execution(
        AgentExecutionRequestV2(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
            run_id,
            workflow.revision_hash,
            "build",
            started.run.agent_bindings[0],
            operational_identity,
            job,
        )
    )


def argument_after(arguments: Sequence[str], flag: str) -> str:
    return arguments[arguments.index(flag) + 1]


def workspace_tool_flags(settings: GrokSubscriptionSettings) -> tuple[str, ...]:
    """Every flag the real workspace-tool invocation carries, read off that invocation."""

    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    try:
        command = executor.prepare_process(subscription_request())
        seen: list[str] = []
        for argument in command.arguments:
            if argument.startswith("--") and argument not in seen:
                seen.append(argument)
        return tuple(seen)
    finally:
        executor.close()


def parsing_grok(known: Iterable[str], *, refuses_unknown: bool = True) -> str:
    """A fake CLI that reads exactly these flags, then refuses a call with no credentials.

    That is the measured order of the real one: an argument it cannot read ends
    the call before anything else, and a call whose arguments it read whole is
    still refused when no credentials arrive.
    """

    return (
        "import sys\n"
        f"known = {sorted(set(known))!r}\n"
        f"refuses_unknown = {refuses_unknown!r}\n"
        "if '--version' in sys.argv:\n"
        "    print('grok 1.0.5 (5115b46bc9) [stable]')\n"
        "    raise SystemExit(0)\n"
        "for argument in sys.argv[1:]:\n"
        "    if refuses_unknown and argument.startswith('--') "
        "and argument not in known:\n"
        '        sys.stderr.write("error: unexpected argument \'" + argument + "\' found\\n")\n'
        "        raise SystemExit(2)\n"
        "sys.stderr.write('Error: Not signed in.\\n')\n"
        "raise SystemExit(1)\n"
    )


def test_the_workspace_tool_factory_offers_its_own_identity_and_only_its_capability(
    tmp_path: Path,
) -> None:
    """A second operation of one provider, not a later revision of the first."""

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    factory = GrokWorkspaceToolExecutorFactory(settings)
    tool_free = GrokSubscriptionExecutorFactory(settings)

    assert factory.key == GROK_WORKSPACE_TOOLS_EXECUTOR_KEY
    assert tool_free.key == GROK_SUBSCRIPTION_EXECUTOR_KEY
    assert tool_free.operational_identity == AgentExecutorOperationalIdentity(
        "headless-print-json-output-schema/v3"
    )
    assert factory.key.provider_id == tool_free.key.provider_id
    assert factory.key.executor_revision != tool_free.key.executor_revision
    assert factory.operational_identity != tool_free.operational_identity
    assert factory.declared_capabilities == frozenset(
        {AgentExecutionCapability.HEADLESS_WITH_TOOLS}
    )
    assert factory.open().close() is None


def test_the_tool_invocation_names_its_tools_and_keeps_every_other_containment_flag(
    tmp_path: Path,
) -> None:
    """One decision separates the two invocations, and it is the tool grant.

    The tool-free call's `--tools=` is gone. In its place: `--tools` names the
    built-in IDs and `--allow` names the five permission classes, because Grok
    splits the two switches Claude combines. Every other flag that call carries
    is still carried, and the environment is the same shape: what changed is
    what the process may touch, not who it is.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    tool_free = GrokSubscriptionExecutorFactory(settings).open()
    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    request = subscription_request(model="grok-4", job=b"draw the owl")

    command = executor.prepare_process(request)
    tool_free_command = tool_free.prepare_process(request)

    assert "--tools=" in tool_free_command.arguments
    assert "--tools=" not in command.arguments
    assert argument_after(command.arguments, "--tools") == ",".join(WORKSPACE_TOOLS)
    allows = [
        command.arguments[index + 1]
        for index, argument in enumerate(command.arguments)
        if argument == "--allow"
    ]
    assert allows == list(WORKSPACE_ALLOW_RULES)
    denies = [
        command.arguments[index + 1]
        for index, argument in enumerate(command.arguments)
        if argument == "--deny"
    ]
    assert denies == list(WORKSPACE_DENY_RULES)
    assert "--deny" not in tool_free_command.arguments
    assert "--always-approve" not in command.arguments
    assert argument_after(command.arguments, "--permission-mode") == "bypassPermissions"
    assert "dontAsk" not in command.arguments
    assert argument_after(tool_free_command.arguments, "--permission-mode") == "dontAsk"
    kept = tuple(
        flag
        for flag in tool_free_command.arguments
        if flag.startswith("--") and flag != "--tools="
    )
    assert all(flag in command.arguments for flag in kept)
    assert argument_after(command.arguments, "--max-turns") == argument_after(
        tool_free_command.arguments, "--max-turns"
    )
    tool_home = dict(command.environment)
    free_home = dict(tool_free_command.environment)
    assert tuple(name for name, _value in command.environment) == tuple(
        name for name, _value in tool_free_command.environment
    )
    assert tool_home["PATH"] == free_home["PATH"] == settings.search_path
    assert tool_home["HOME"] == tool_home["GROK_HOME"]
    assert free_home["HOME"] == free_home["GROK_HOME"]
    assert Path(tool_home["HOME"]).parent == settings.workspace
    assert Path(free_home["HOME"]).parent == settings.workspace
    assert tool_home["HOME"] != free_home["HOME"]
    assert all(argument for argument in command.arguments)

    workspace = leased_workspace(tmp_path)
    result = executor.decode_process_completion(
        leased(command, workspace), launched(command, workspace)
    )

    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][1:] == list(command.arguments[1:])
    assert observed["working_directory"] == str(workspace)
    assert observed["job"] == "draw the owl"
    executor.release_credential_channel(command)
    tool_free.release_credential_channel(tool_free_command)
    tool_free.close()
    executor.close()


@pytest.mark.proves("a-pinned-budget-turn-bound-is-the-tool-attempt-ceiling")
def test_a_workspace_tool_call_takes_the_pinned_turn_bound(
    tmp_path: Path,
) -> None:
    """The budget names the ceiling; without one the existing default stays.

    The tool-free call is the other reader: it keeps sixteen even when the
    same request carries a bound. Removing the request field from the tool
    vector makes the pinned case agree with the default.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    tool_free = GrokSubscriptionExecutorFactory(settings).open()

    default_command = executor.prepare_process(subscription_request())
    pinned_command = executor.prepare_process(
        subscription_request(maximum_assistant_turns=8)
    )
    headless = tool_free.prepare_process(
        subscription_request(maximum_assistant_turns=8)
    )

    assert argument_after(default_command.arguments, "--max-turns") == "16"
    assert argument_after(pinned_command.arguments, "--max-turns") == "8"
    assert argument_after(headless.arguments, "--max-turns") == "16"
    executor.release_credential_channel(default_command)
    executor.release_credential_channel(pinned_command)
    tool_free.release_credential_channel(headless)
    tool_free.close()
    executor.close()


def test_a_non_subscription_profile_reaches_no_tool_bearing_process(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokWorkspaceToolExecutorFactory(settings).open()

    with pytest.raises(GrokSubscriptionAuthModeUnsupported, match="workspace-tool"):
        executor.prepare_process(subscription_request(auth_mode=AuthMode.API_KEY))


@pytest.mark.parametrize(
    ("executor_revision", "requested_capability"),
    [
        pytest.param(
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            id="the tool-free executor is asked for tools",
        ),
        pytest.param(
            GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS,
            id="the tool executor is asked for a tool-free call",
        ),
    ],
)
def test_a_binding_asking_a_grok_executor_for_a_capability_it_never_declared_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_root_outside_a_worktree: Path,
    executor_revision: AgentExecutorRevision,
    requested_capability: AgentExecutionCapability,
) -> None:
    """Neither executor answers the other's ask, and the refusal is the starter's."""

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    runtime = grok_subscription_runtime(
        tmp_path,
        settings,
        scratch_root_outside_a_worktree,
        workspace_tools=True,
    )
    runtime.initialize_storage()

    def unexpected_enqueue(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an undeclared capability reached the durable queue")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", unexpected_enqueue)
    try:
        refused, _workflow = grok_subscription_start(
            runtime,
            "grok/undeclared-capability",
            requested_capability=requested_capability,
            executor_revision=executor_revision,
        )
        with runtime.engine.connect() as connection:
            written = tuple(
                connection.scalar(sa.select(sa.func.count()).select_from(table))
                for table in (runs, run_agent_bindings)
            )
    finally:
        runtime.close()

    assert isinstance(refused, DurableAgentExecutorCapabilityUnavailable)
    assert written == (0, 0)


def test_a_node_requesting_grok_tools_starts_through_the_production_starter(
    tmp_path: Path, scratch_root_outside_a_worktree: Path
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    runtime = grok_subscription_runtime(
        tmp_path,
        settings,
        scratch_root_outside_a_worktree,
        workspace_tools=True,
    )
    runtime.initialize_storage()
    try:
        started, _workflow = grok_subscription_start(
            runtime,
            "grok/tool-capability",
            requested_capability=AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            executor_revision=GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
        )
        with runtime.engine.connect() as connection:
            started_runs = connection.scalar(
                sa.select(sa.func.count()).select_from(runs)
            )
    finally:
        runtime.close()

    assert isinstance(started, DurableRunCreated)
    assert started_runs == 1


def test_a_tool_free_grok_attempt_persists_the_v2_operational_identity(
    tmp_path: Path, scratch_root_outside_a_worktree: Path
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    runtime = grok_subscription_runtime(
        tmp_path, settings, scratch_root_outside_a_worktree
    )
    runtime.initialize_storage()
    try:
        execution = grok_subscription_attempt(
            runtime,
            "grok/tool-free-identity",
            requested_capability=AgentExecutionCapability.HEADLESS,
            executor_revision=GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            operational_identity=GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
        )
        workspaces = runtime_workspace_owner(runtime)
        outcome = execute_agent_attempt(
            execution,
            GrokSubscriptionExecutorFactory(settings).open(),
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            workspaces,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )
        with runtime.engine.connect() as connection:
            receipts = connection.execute(sa.select(agent_receipts_v2)).mappings().all()
    finally:
        runtime.close()

    assert isinstance(outcome, AgentAttemptSucceeded)
    assert len(receipts) == 1
    assert receipts[0]["executor_revision"] == (
        GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value
    )
    assert receipts[0]["executor_operational_identity"] == (
        "headless-print-json-output-schema/v3"
    )


@pytest.mark.parametrize(
    (
        "requested_capability",
        "executor_revision",
        "operational_identity",
        "workspace_tools",
    ),
    (
        (
            AgentExecutionCapability.HEADLESS,
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
            False,
        ),
        (
            AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
            GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY,
            True,
        ),
    ),
)
def test_a_grok_job_above_the_measured_bound_is_refused_before_any_provider_launch(
    tmp_path: Path,
    scratch_root_outside_a_worktree: Path,
    requested_capability: AgentExecutionCapability,
    executor_revision: AgentExecutorRevision,
    operational_identity: AgentExecutorOperationalIdentity,
    workspace_tools: bool,
) -> None:
    """A composed job over the 30_000-byte prompt bound is refused before launch.

    MAXIMUM_INSTRUCTION_BYTES (16_384) is the document's own authoring bound
    on one node's instruction text, deliberate and unrelated to this test's
    subject; it is not relaxed here. Under V3 the bytes an agent reads are the
    instruction plus its orders (compose_node_job.node_job), so a job past
    _MEASURED_INLINE_PROMPT_BYTES (30_000) is reached legally by adding one
    order near the door's own MAXIMUM_INSTANCE_DOCUMENT_BYTES bound rather
    than by growing the instruction alone (#901 slice 5, #934).
    """
    order_name = "context"
    instruction = "x" * 15_000
    order_value = json.dumps("a" * 16_000).encode("utf-8")
    assert len(order_value) <= MAXIMUM_INSTANCE_DOCUMENT_BYTES

    settings = grok_subscription_deployment(
        tmp_path, "raise AssertionError('a Grok process was launched')\n"
    )
    runtime = grok_subscription_runtime(
        tmp_path,
        settings,
        scratch_root_outside_a_worktree,
        workspace_tools=workspace_tools,
    )
    runtime.initialize_storage()
    try:
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        auth = AuthProfileRevision(
            "grok-tools", 1, ProviderId("xai"), AuthMode.SUBSCRIPTION
        )
        catalog.publish_auth_profile_revision(auth)
        configuration = AgentConfigurationRevision(
            "grok-4",
            auth.revision_hash,
            executor_revision,
            requested_capability,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        catalog.publish_agent_configuration_revision(configuration)
        publish_checked_model_registry(
            runtime.engine, ProviderId("xai"), (configuration,)
        )
        DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
        workflow = WorkflowRevision(
            declaring_mode_of(
                f"""format_version: 3
name: Grok subscription prelaunch-bound document
graph_inputs:
  - name: {order_name}
    schema: {{ref: result-schema, revision: {ANY_JSON_SCHEMA.revision_hash.value}}}
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: {instruction}
    inputs:
      - name: {order_name}
        from: {{graph_input: {order_name}}}
    outputs:
      - name: result
        schema: {{ref: result-schema, revision: {ANY_JSON_SCHEMA.revision_hash.value}}}
""".encode(),
                requested_capability,
            )
        )
        DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
        run_name = f"grok/prelaunch-bound/{requested_capability.value}"
        run_id = RunId(run_name)
        started = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV3(
                run_id,
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
                ),
                orders=(AuthoredOrder(order_name, InlineOrderValue(order_value)),),
            )
        )
        assert isinstance(started, DurableRunCreated)
        assert isinstance(started.run, RunV3)

        job = node_job(
            instruction,
            orders=(
                RunInput(
                    order_name,
                    ANY_JSON_SCHEMA.revision_hash,
                    order_value,
                ),
            ),
            composition_version=NodeJobCompositionVersion.CURRENT,
        ).encode("utf-8")
        assert len(job) > 30_000  # grok_subscription._MEASURED_INLINE_PROMPT_BYTES

        execution = agent_attempt_execution(
            AgentExecutionRequestV2(
                NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
                run_id,
                workflow.revision_hash,
                "build",
                started.run.agent_bindings[0],
                operational_identity,
                job,
            )
        )
        executor = (
            GrokWorkspaceToolExecutorFactory(settings).open()
            if workspace_tools
            else GrokSubscriptionExecutorFactory(settings).open()
        )
        outcome = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )
    finally:
        runtime.close()

    assert isinstance(outcome, AgentAttemptFailed)
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.AGENT_REFUSED
    assert list(settings.workspace.iterdir()) == []


def test_a_tool_bearing_grok_attempt_writes_in_its_lease_and_answers_what_it_wrote(
    tmp_path: Path, scratch_root_outside_a_worktree: Path
) -> None:
    """The vertical the capability exists for, through the production path."""

    settings = grok_subscription_deployment(tmp_path, TOOL_USING_GROK)
    runtime = grok_subscription_runtime(
        tmp_path,
        settings,
        scratch_root_outside_a_worktree,
        workspace_tools=True,
    )
    runtime.initialize_storage()
    try:
        execution = grok_subscription_attempt(
            runtime,
            "grok/workspace-tools",
            requested_capability=AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            executor_revision=GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
            operational_identity=GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY,
        )
        workspaces = runtime_workspace_owner(runtime)
        leased_directory = workspaces.scratch_root / execution.attempt_id.value
        outcome = execute_agent_attempt(
            execution,
            GrokWorkspaceToolExecutorFactory(settings).open(),
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            workspaces,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )
        with runtime.engine.connect() as connection:
            receipts = connection.execute(sa.select(agent_receipts_v2)).mappings().all()
    finally:
        runtime.close()

    assert isinstance(outcome, AgentAttemptSucceeded)
    assert len(receipts) == 1
    assert receipts[0]["executor_revision"] == (
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value
    )
    assert receipts[0]["executor_operational_identity"] == (
        GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY.value
    )
    answered = json.loads(receipts[0]["output_bytes"])
    assert answered["wrote"] == str(leased_directory / WORKSPACE_ARTEFACT_NAME)
    assert answered["read_back"] == WORKSPACE_ARTEFACT_LINE
    assert not leased_directory.exists()


def recorded_grok_stream(*lines: Mapping[str, object]) -> bytes:
    """These lines exactly as grok writes a stream: NDJSON, no trailing feed."""

    return "\n".join(json.dumps(line) for line in lines).encode("utf-8")


RECORDED_SESSION_ID = "01a06d2b-2e69-7390-a807-06901b3a2191"
RECORDED_MODEL = "grok-4.6"
RECORDED_GRANTED_TOOLS = ["run_terminal_command", "read_file", "search_replace"]
RECORDED_PERMISSION_MODE = "bypassPermissions"
RECORDED_SESSION_HEADER_NOISE = ("uuid", "slash_commands", "cwd")
"""What the header says beside those facts, and what no transcript needs."""

NONEMPTY_STRING_SCHEMA = (
    Path(__file__).parents[2] / "workflows" / "schemas" / "nonempty_string.json"
).read_bytes()
"""The published schema the provider-canary vector declares for its answer."""


def recorded_session_start() -> dict[str, object]:
    """The `system` line every measured workspace-tool stream opens with."""

    return {
        "type": "system",
        "subtype": "init",
        "session_id": RECORDED_SESSION_ID,
        "model": RECORDED_MODEL,
        "tools": RECORDED_GRANTED_TOOLS,
        "permissionMode": RECORDED_PERMISSION_MODE,
        "cwd": "/tmp/probe/repo",
        "slash_commands": ["compact", "context"],
        "mcp_servers": [],
        "skills": [],
        "uuid": "48671245-2fa0-4512-94c0-ff3311de4bca",
    }


def recorded_assistant_message(*blocks: Mapping[str, object]) -> dict[str, object]:
    """One whole assistant message, in the shape the measured stream carries."""

    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": list(blocks)},
    }


def recorded_tool_results(*blocks: Mapping[str, object]) -> dict[str, object]:
    """What the doors answered, as the stream's own `user` message."""

    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


def recorded_terminal_line(
    answer: str | None = None,
    is_error: object = False,
    subtype: str = "success",
) -> dict[str, object]:
    """The `result` line grok names its own session's end with.

    `is_error` is deliberately untyped: a release that spells its own flag as
    something other than the JSON boolean is one of the endings this file has
    to be able to write down. An absent `answer` leaves the `result` field off
    the line entirely, which is how a terminal line carrying no last words
    arrives.
    """

    terminal: dict[str, object] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "num_turns": 1,
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 16374,
            "output_tokens": 641,
            "cache_read_input_tokens": 2688,
            "cache_creation_input_tokens": 0,
        },
    }
    if answer is not None:
        terminal["result"] = answer
    return terminal


def recorded_door_opening_stream(header: Mapping[str, object], answer: str) -> bytes:
    """The shortest whole session: this header, one door opened, this answer."""

    call_id = "call-4f1c2f10-7b2a-4a55-9a2f-1d0b2c3e4f50-0"
    return recorded_grok_stream(
        header,
        recorded_assistant_message(
            {
                "type": "tool_use",
                "id": call_id,
                "name": "search_replace",
                "input": {"file_path": "README.md"},
            }
        ),
        recorded_tool_results(
            {"type": "tool_result", "tool_use_id": call_id, "content": "written"}
        ),
        recorded_terminal_line(answer),
    )


RECORDED_ANSWER = '{"summary":"Appended the probe line.","changed_paths":["README.md"]}'
"""The answer the measured tool-using session ended on, as its own text."""

RECORDED_NARRATION = "I'll read the workspace guidance, then append the line."
"""What the same session said before acting, once no schema constrained it."""

RECORDED_THOUGHT = "Read AGENTS.md and README.md first."
"""What it thought before saying that, signature stripped."""

RECORDED_PREAMBLE = '{"summary":"Reading AGENTS.md and README.md.","changed_paths":[]}'
"""The report-shaped narration a collapsed session ends on instead."""


def decoded_workspace_tool_stream(
    tmp_path: Path,
    stream: bytes,
    declared_output_schema: bytes | None = None,
) -> AgentExecutionResult | AgentExecutionFailure:
    """One workspace-tool call decoded from exactly these recorded stream bytes."""

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    try:
        command = executor.prepare_process(
            subscription_request(declared_output_schema=declared_output_schema)
        )
        try:
            return executor.decode_process_completion(
                leased(command, leased_workspace(tmp_path)),
                AgentProcessCompletion(0, stream, b""),
            )
        finally:
            executor.release_credential_channel(command)
    finally:
        executor.close()


def test_the_tool_vector_asks_for_the_stream_the_tool_free_one_does_not(
    tmp_path: Path,
) -> None:
    """The wire is what separates the two operations' identities (#1165, #1174)."""

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    tool_free = GrokSubscriptionExecutorFactory(settings).open()
    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    request = subscription_request()

    command = executor.prepare_process(request)
    tool_free_command = tool_free.prepare_process(request)

    assert argument_after(command.arguments, "--output-format") == (
        "streaming-messages-json"
    )
    assert argument_after(tool_free_command.arguments, "--output-format") == "json"
    assert GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY == (
        AgentExecutorOperationalIdentity(
            "headless-workspace-tools-streaming-messages-json-schema-in-job/v4"
        )
    )
    executor.release_credential_channel(command)
    tool_free.release_credential_channel(tool_free_command)
    executor.close()
    tool_free.close()


def test_the_tool_vector_carries_its_declared_schema_in_the_job_not_in_a_flag(
    tmp_path: Path,
) -> None:
    """The move #1174 was cut for: the shape is asked for, never enforced per turn.

    `--json-schema` constrains every assistant message, so an operation that
    narrates and acts before it answers ends on its own preamble. This vector
    therefore carries no such flag while its tool-free sibling still does, and
    the schema the node declared reaches the model as the job's closing words --
    the same published document bytes the output seam judges the answer against.
    """

    schema = b'{"type":"object","required":["summary"]}'
    job = b"Append one line to README.md"
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    tool_free = GrokSubscriptionExecutorFactory(settings).open()
    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    request = subscription_request(job=job, declared_output_schema=schema)

    command = executor.prepare_process(request)
    tool_free_command = tool_free.prepare_process(request)

    assert "--json-schema" not in command.arguments
    assert argument_after(tool_free_command.arguments, "--json-schema") == (
        schema.decode("utf-8")
    )
    carried = argument_after(command.arguments, "-p")
    assert carried.startswith(job.decode("utf-8"))
    assert carried.endswith(schema.decode("utf-8"))
    executor.release_credential_channel(command)
    tool_free.release_credential_channel(tool_free_command)
    executor.close()
    tool_free.close()


def test_a_tool_vector_job_without_a_declared_schema_carries_the_job_alone(
    tmp_path: Path,
) -> None:
    """Nothing is asked for where the node declared no shape to ask for."""

    job = b"Append one line to README.md"
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokWorkspaceToolExecutorFactory(settings).open()

    command = executor.prepare_process(subscription_request(job=job))

    assert argument_after(command.arguments, "-p") == job.decode("utf-8")
    executor.release_credential_channel(command)
    executor.close()


def test_a_job_the_declared_schema_pushes_past_the_transport_bound_is_refused(
    tmp_path: Path,
) -> None:
    """The measured bound is about the carrier, not about who wrote which part.

    A job that fits alone and no longer fits once the schema closes it is a
    prompt this transport was never measured with, so it is refused before a
    process exists rather than sent truncated.
    """

    # grok_subscription._MEASURED_INLINE_PROMPT_BYTES
    measured_inline_prompt_bytes = 30_000
    schema = b'{"type":"object","required":["summary"]}'
    job = b"j" * (measured_inline_prompt_bytes - len(schema))
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokWorkspaceToolExecutorFactory(settings).open()

    fitting = executor.prepare_process(subscription_request(job=job))
    with pytest.raises(AgentExecutionPreflightRefusal, match="30,000") as refused:
        executor.prepare_process(
            subscription_request(job=job, declared_output_schema=schema)
        )

    assert refused.value.code is AgentAttemptFailureCode.AGENT_REFUSED
    executor.release_credential_channel(fitting)
    executor.close()


def test_a_tool_using_session_keeps_its_doors_and_answers_with_its_last_output(
    tmp_path: Path,
) -> None:
    """The steps that reached the answer, in the order grok wrote them.

    Replayed from the capture measured 04.09.2026 on grok 1.0.5 / grok-4.6
    without `--json-schema` (#1174): the session narrates in free prose, opens
    its doors, and ends on one bare JSON document as the terminal `result` --
    with no `structured_output` field on that line at all. Every line of it has
    a name here: the header keeps the session, the model, the granted doors and
    the regime they were granted under, a thinking block is one of the agent's
    turns without its signature blob, and the terminal line keeps what the call
    spent.
    """

    read_call = {
        "type": "tool_use",
        "id": "call-71a284dc-0",
        "name": "read_file",
        "input": {"target_file": "README.md"},
    }
    stream = recorded_grok_stream(
        recorded_session_start(),
        recorded_assistant_message(
            {"type": "thinking", "thinking": RECORDED_THOUGHT, "signature": "s" * 400},
            {"type": "text", "text": RECORDED_NARRATION},
            read_call,
        ),
        recorded_tool_results(
            {
                "type": "tool_result",
                "tool_use_id": "call-71a284dc-0",
                "content": "# Probe repo",
                "is_error": False,
            }
        ),
        recorded_assistant_message({"type": "text", "text": RECORDED_ANSWER}),
        recorded_terminal_line(RECORDED_ANSWER),
    )

    result = decoded_workspace_tool_stream(
        tmp_path, stream, declared_output_schema=b'{"type":"object"}'
    )

    assert isinstance(result, AgentExecutionResult)
    assert result.output_bytes == RECORDED_ANSWER.encode()
    assert result.transcript is not None
    header, *steps = result.transcript.events
    assert isinstance(header, UnrecognisedProviderOutput)
    assert all(
        named in header.text
        for named in (
            RECORDED_SESSION_ID,
            RECORDED_MODEL,
            RECORDED_PERMISSION_MODE,
            *RECORDED_GRANTED_TOOLS,
        )
    )
    assert not any(noise in header.text for noise in RECORDED_SESSION_HEADER_NOISE)
    assert tuple(steps) == (
        AssistantTurn(RECORDED_THOUGHT),
        AssistantTurn(RECORDED_NARRATION),
        ToolCalled("read_file", '{"target_file":"README.md"}'),
        ToolReturned("read_file", "# Probe repo"),
        AssistantTurn(RECORDED_ANSWER),
        Usage(16374, 641, 2688, 0),
    )


def test_a_session_that_ended_before_its_first_tool_call_is_no_answer_at_all(
    tmp_path: Path,
) -> None:
    """The collapse #1165 was cut for, and the net that outlives its cause.

    Dropping `--json-schema` (#1174) removes what pressed a preamble into the
    report form, but a session that answers a tools binding without opening one
    door is still an answer to a call that never happened. A binding that asked
    for tools therefore gets a typed provider refusal, never that preamble as a
    candidate report -- and the preamble stays in the transcript, where a reader
    can see what ended the attempt.
    """

    stream = recorded_grok_stream(
        recorded_session_start(),
        recorded_assistant_message(
            {"type": "thinking", "thinking": "Read AGENTS.md first.", "signature": "s"},
            {"type": "text", "text": RECORDED_PREAMBLE},
        ),
        recorded_terminal_line(RECORDED_PREAMBLE),
    )

    result = decoded_workspace_tool_stream(
        tmp_path, stream, declared_output_schema=b'{"type":"object"}'
    )

    assert isinstance(result, GrokProviderEndedWithoutToolUse)
    assert result.code is AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
    assert result.transcript is not None
    assert AssistantTurn(RECORDED_PREAMBLE) in result.transcript.events


def test_a_root_string_schema_answer_survives_the_stream(tmp_path: Path) -> None:
    """The provider-canary vector: a schema whose root is a string, not an object.

    Replayed from a billed run of that vector's own instruction and schema
    without `--json-schema` (04.09.2026, grok 1.0.5 / grok-4.6, `#1174`). It
    settles what an object-schema capture could not: asked in words for a bare
    root-string document, the session narrated freely, ran its command, and
    ended on that string's JSON text -- quotes included, no fence around it --
    as the terminal `result`. Those are the bytes the output seam judges, and
    they are a `nonempty_string` document.
    """

    command = "sleep 20 && echo canary-alive"
    call_id = "call-78cc785d-e7bf-4f49-9de0-5e4d219c1d4e-0"
    stream = recorded_grok_stream(
        recorded_session_start(),
        recorded_assistant_message(
            {"type": "text", "text": "I'll run that exact command."},
            {
                "type": "tool_use",
                "id": call_id,
                "name": "run_terminal_command",
                "input": {
                    "command": command,
                    "description": "Sleep 20 seconds then echo canary-alive",
                    "timeout": 30000,
                },
            },
        ),
        recorded_tool_results(
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": json.dumps(
                    {
                        "type": "Bash",
                        "output_for_prompt": "exit: 0\ncanary-alive\n",
                        "exit_code": 0,
                        "command": command,
                    }
                ),
            }
        ),
        recorded_terminal_line('"canary-alive"'),
    )

    result = decoded_workspace_tool_stream(
        tmp_path, stream, declared_output_schema=NONEMPTY_STRING_SCHEMA
    )

    assert isinstance(result, AgentExecutionResult)
    assert result.output_bytes == b'"canary-alive"'
    schema = read_schema_document(NONEMPTY_STRING_SCHEMA)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(
        read_instance_document(result.output_bytes, schema), InstanceAccepted
    )


def test_a_fenced_answer_reaches_the_output_seam_exactly_as_written(
    tmp_path: Path,
) -> None:
    """Nothing is stripped at this seam, so a fence is the schema's to refuse.

    The ask spells out that the answer wears no code fence, and neither measured
    session wrote one -- but an executor that quietly unwrapped a fence would be
    deciding what counts as an answer, and that decision belongs to the declared
    schema alone. So the fenced text reaches the output seam byte for byte, and
    the seam refuses it as the JSON document it is not.
    """

    fenced = f"```json\n{RECORDED_ANSWER}\n```"
    declared_schema = b'{"type":"object"}'

    result = decoded_workspace_tool_stream(
        tmp_path,
        recorded_door_opening_stream(recorded_session_start(), fenced),
        declared_output_schema=declared_schema,
    )

    assert isinstance(result, AgentExecutionResult)
    assert result.output_bytes == fenced.encode("utf-8")
    schema = read_schema_document(declared_schema)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(
        read_instance_document(result.output_bytes, schema), InstanceRefused
    )


def test_a_session_header_this_reader_cannot_reduce_survives_whole(
    tmp_path: Path,
) -> None:
    """A release that renames the header's fields loses its noise, not its line.

    The reduction keeps the named facts and drops the rest, so a header that
    names none of them reduces to nothing -- and an empty step is exactly how a
    line this reader does recognise goes missing. It keeps the whole line then.
    """

    renamed_header = {
        "type": "system",
        "subtype": "init",
        "sessionId": RECORDED_SESSION_ID,
        "modelName": RECORDED_MODEL,
    }

    result = decoded_workspace_tool_stream(
        tmp_path,
        recorded_door_opening_stream(renamed_header, RECORDED_ANSWER),
        declared_output_schema=b'{"type":"object"}',
    )

    assert isinstance(result, AgentExecutionResult)
    assert result.transcript is not None
    header = result.transcript.events[0]
    assert isinstance(header, UnrecognisedProviderOutput)
    assert RECORDED_SESSION_ID in header.text
    assert RECORDED_MODEL in header.text


@pytest.mark.parametrize(
    ("last_line", "why"),
    (
        pytest.param(None, "the call died before naming its own end", id="no-terminal"),
        pytest.param(
            recorded_terminal_line("", is_error=True),
            "the provider ended the call itself",
            id="provider-error",
        ),
        pytest.param(
            recorded_terminal_line("canary-alive", is_error=0),
            "an ending that is not the JSON boolean is no successful ending",
            id="error-flag-is-no-boolean",
        ),
        pytest.param(
            recorded_terminal_line(),
            "a terminal line without last words leaves the schema-free path none",
            id="no-result-text",
        ),
        pytest.param(
            recorded_terminal_line(""),
            "empty last words are no answer either",
            id="empty-result-text",
        ),
    ),
)
def test_a_stream_without_a_usable_terminal_line_has_no_final_message(
    tmp_path: Path, last_line: Mapping[str, object] | None, why: str
) -> None:
    """The end is read from the stream's own terminal line, never guessed."""

    lines: tuple[Mapping[str, object], ...] = (
        recorded_session_start(),
        recorded_assistant_message(
            {
                "type": "tool_use",
                "id": "call-0",
                "name": "read_file",
                "input": {"target_file": "README.md"},
            }
        ),
    )
    if last_line is not None:
        lines = (*lines, last_line)

    result = decoded_workspace_tool_stream(tmp_path, recorded_grok_stream(*lines))

    assert isinstance(result, GrokProviderEndedWithoutFinalMessage), why
    assert result.transcript is not None
    assert ToolCalled("read_file", '{"target_file":"README.md"}') in (
        result.transcript.events
    )


def test_a_session_the_provider_ended_says_so_in_its_own_words(
    tmp_path: Path,
) -> None:
    """A refused ending keeps the why, not only what the call spent.

    The terminal line is the one place an in-band refusal is stated, and its
    usage record parses on that same line -- so reading only the spend would
    leave a transcript that says what an attempt cost and never that the
    provider, rather than this process, ended it.
    """

    stream = recorded_grok_stream(
        recorded_session_start(),
        recorded_terminal_line(
            "Maximum turns reached", is_error=True, subtype="error_max_turns"
        ),
    )

    result = decoded_workspace_tool_stream(tmp_path, stream)

    assert isinstance(result, GrokProviderEndedWithoutFinalMessage)
    assert result.transcript is not None
    assert (
        ProviderTerminalRefusal("error_max_turns", "", "Maximum turns reached")
        in result.transcript.events
    )
    assert Usage(16374, 641, 2688, 0) in result.transcript.events


def test_a_call_that_wrote_no_stream_keeps_what_it_did_write(
    tmp_path: Path,
) -> None:
    """A crash writes a traceback where a stream belonged, and it is evidence."""

    crash = "Traceback (most recent call last):\n  RuntimeError: no session\n"

    result = decoded_workspace_tool_stream(tmp_path, crash.encode("utf-8"))

    assert isinstance(result, GrokProviderEndedWithoutFinalMessage)
    assert result.transcript == AttemptTranscript.of(
        [
            UnrecognisedProviderOutput("Traceback (most recent call last):"),
            UnrecognisedProviderOutput("  RuntimeError: no session"),
        ]
    )


def test_an_answer_past_the_durable_output_bound_fails_the_attempt(
    tmp_path: Path,
) -> None:
    """The stream may end well and still carry more than durable state admits."""

    oversized = "a" * (MAXIMUM_AGENT_OUTPUT_BYTES_V2 + 1)
    stream = recorded_grok_stream(
        recorded_session_start(),
        recorded_assistant_message(
            {
                "type": "tool_use",
                "id": "call-0",
                "name": "read_file",
                "input": {"target_file": "README.md"},
            }
        ),
        recorded_terminal_line(oversized),
    )

    result = decoded_workspace_tool_stream(tmp_path, stream)

    assert not isinstance(result, AgentExecutionResult)
    assert result.code is AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY


def test_an_executable_that_starts_this_exact_grok_invocation_is_attested(
    tmp_path: Path,
) -> None:
    reference = grok_named_deployment(tmp_path, "reference", INTROSPECTING_GROK)
    settings = grok_named_deployment(
        tmp_path, "deployment", parsing_grok(workspace_tool_flags(reference))
    )

    assert attest_grok_workspace_tool_invocation(settings) is None


def test_a_deployment_whose_host_cannot_fence_this_vector_is_refused_at_composition(
    tmp_path: Path,
) -> None:
    """No quiet degradation: the tool-bearing vector is armed because its child
    is held inside a grant, so a host that cannot draw one serves it not at all
    rather than serving it open."""

    fenceless = tmp_path / "fenceless"
    fenceless.mkdir()
    settings = replace(
        grok_named_deployment(tmp_path, "deployment", INTROSPECTING_GROK),
        search_path=str(fenceless),
    )

    with pytest.raises(GrokExecutableUnsupported, match="bwrap"):
        attest_grok_workspace_tool_invocation(settings)


def test_an_executable_missing_any_flag_of_this_grok_invocation_is_refused_by_that_flag(
    tmp_path: Path,
) -> None:
    """Every flag of the vector is a containment decision, so every one is probed."""

    reference = grok_named_deployment(tmp_path, "reference", INTROSPECTING_GROK)
    flags = workspace_tool_flags(reference)

    assert flags
    for missing in flags:
        settings = grok_named_deployment(
            tmp_path,
            f"without{flags.index(missing)}",
            parsing_grok(flag for flag in flags if flag != missing),
        )

        with pytest.raises(GrokExecutableUnsupported, match=re.escape(missing)):
            attest_grok_workspace_tool_invocation(settings)


def test_an_executable_that_never_names_an_unexpected_argument_cannot_be_attested(
    tmp_path: Path,
) -> None:
    """Without the control, "said nothing" and "has nothing to say" look alike."""

    reference = grok_named_deployment(tmp_path, "reference", INTROSPECTING_GROK)
    settings = grok_named_deployment(
        tmp_path,
        "deployment",
        parsing_grok(workspace_tool_flags(reference), refuses_unknown=False),
    )

    with pytest.raises(GrokExecutableUnsupported, match="no release can know"):
        attest_grok_workspace_tool_invocation(settings)


def test_an_executable_that_answers_a_jobless_grok_invocation_successfully_is_refused(
    tmp_path: Path,
) -> None:
    """The probe rests on that call being refused, so a success is unmeasured ground."""

    settings = grok_subscription_deployment(tmp_path, "raise SystemExit(0)\n")

    with pytest.raises(GrokExecutableUnsupported, match="jobless"):
        attest_grok_workspace_tool_invocation(settings)


def test_an_executable_that_answers_its_version_and_cannot_spawn_is_refused(
    tmp_path: Path,
) -> None:
    """The gap this attestation exists for: a version answer is not startability.

    The refusal quotes whatever the failed start said, and inside the fence
    that sentence is the enforcer's: it is the process that got as far as
    trying to run this executable.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    assert verify_grok_capability(settings.executable) in CONFORMANT_GROK_VERSIONS
    settings.executable.write_text(
        "#!/atelier2/no/such/interpreter\n", encoding="utf-8"
    )
    settings.executable.chmod(0o755)

    with pytest.raises(GrokExecutableUnsupported, match="No such file or directory"):
        attest_grok_workspace_tool_invocation(settings)
