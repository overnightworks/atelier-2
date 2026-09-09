"""One headless `grok` subscription executor.

Containment flags are measured against grok 1.0.4. On grok 1.0.5 / grok-4.6,
ten synthetic schema calls put one unique token halfway through a 10,000,
20,000, 30,000, 40,000 or 50,000-byte prompt. Both `--prompt-file` and inline
`-p` echoed it at 10,000, 20,000 and 30,000 bytes (file: 2.966, 2.968 and
8.234 s; inline: 2.774, 3.209 and 7.542 s). Inline returned schema-valid
non-token placeholders at 40,000 and 50,000 bytes (5.345 and 5.520 s); the
file carrier returned one only after narrated file reading at 40,000 (16.156
s) and a placeholder at 50,000 (5.460 s). A later 50,009-byte inline call
ended successfully in 3.527 s with `{"findings":[],"verdict":"revise"}`.
This tool-free adapter therefore uses inline `-p` only through the measured
30,000-byte bound and returns typed `AGENT_REFUSED` before launch above it.
Standard input is not a documented prompt carrier. The version gate admits
exactly this measured release. The child environment is `HOME`, `GROK_HOME`,
`PATH`, `SHELL` and `TERM` -- and nothing else. `HOME` is containment, not
convenience: without it the CLI resolves the invoking account's own profile.
`SHELL` and `TERM` are the measured fix for a headless silence-kill: a
`run_terminal_cmd` that stays silent for ~25s cancels the session under the
three-variable environment but survives once a concrete shell and terminal
type are named (issue #642); a real PTY is the structural successor
(issue #943).

Every invocation gets one private disposable `HOME`/`GROK_HOME`. The seam
copies only `auth.json` into it and writes an inert compatibility configuration
with exclusive no-follow opens, then removes the whole home on every lifecycle
path. Provider-created sessions therefore never enter the source credential
directory or outlive their invocation.

Flags alone do not bound what the CLI discovers. `grok inspect --json` reports
the plugins, hooks, MCP servers, skills, marketplaces, LSP servers, permission
sources, project instructions and external compatibility cells a directory
would load. Each prepared invocation attests its own exact home, configuration
and working directory before the supervisor may launch it.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from atelier2.adapters.bounded_processes import (
    bounded_process_answer,
    bounded_process_streams,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agent_transcripts import (
    MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES,
    AssistantTurn,
    AttemptTranscript,
    ProviderTerminalRefusal,
    ToolCalled,
    ToolReturned,
    TranscriptEvent,
    UnrecognisedProviderOutput,
    Usage,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AuthMode,
    ProviderId,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutionPreflightRefusal,
    AgentExecutorKey,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    PrintModeExecutor,
)

GROK_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("xai"), AgentExecutorRevision("grok-subscription/v1")
)
GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-print-json-output-schema/v3"
)

# The largest final envelope one call of either operation may write. It is
# deliberately larger than the durable answer bound: the answer travels inside
# a JSON envelope, where JSON string escaping expands one source byte to at
# most six frame bytes, and the rest is the envelope's own metadata allowance.
GROK_SUBSCRIPTION_ENVELOPE_BYTES = 8 * MAXIMUM_AGENT_OUTPUT_BYTES_V2

# The largest raw standard-output frame one whole call may write, for either
# operation. The two halves are added rather than shared because they bound
# different things -- the final answer, and the story that reached it -- and a
# single number would silently trade one against the other.
#
# The tool-free operation writes one `--output-format json` envelope and
# nothing else. The workspace-tool operation writes an NDJSON stream whose last
# line is a terminal envelope of the same size class, preceded by exactly the
# turns and tool results a transcript may keep: one whole transcript's worth is
# therefore that half's allowance, and a stream past it is one whose steps this
# repository could not have kept whole anyway.
GROK_SUBSCRIPTION_FRAME_BYTES = (
    GROK_SUBSCRIPTION_ENVELOPE_BYTES + MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES
)

CONFORMANT_GROK_VERSIONS = frozenset({(1, 0, 5)})

_VERSION_FLAG = "--version"
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_VERSION_PROBE_OUTPUT_BYTES = 4_096

_OUTPUT_FORMAT_FLAG = "--output-format"
_JSON_OUTPUT_FORMAT = "json"
# The format that publishes the turns and tool calls as well as the answer.
# Measured 04.09.2026 against grok 1.0.5 / grok-4.6 in the workspace-tool
# vector: standard output is pure NDJSON in the Anthropic Messages API wire
# format -- a `system` init line, whole `assistant` and `user` messages
# carrying `text`, `thinking`, `tool_use` and `tool_result` blocks, and last a
# `result` line naming `is_error` and the answer text. Whole messages, not
# deltas: the sibling `streaming-json` writes the same session as one NDJSON
# line per generated token (186 lines and 13,155 bytes for the same four-turn
# task, against 9,891 bytes here) and its terminal line carries no whole answer
# text at all.
# Measured again 04.09.2026 without `--json-schema` (`#1174`): the terminal
# line then carries no `structured_output` field at all, and its `result` is
# the model's last message as bare text -- one JSON document, no code fence and
# no prose around it, in both the object-schema and the root-string vector.
_STREAMING_MESSAGES_JSON_OUTPUT_FORMAT = "streaming-messages-json"
_JSON_SCHEMA_FLAG = "--json-schema"
_MODEL_FLAG = "--model"
_MAXIMUM_TURNS_FLAG = "--max-turns"
# Headless one-answer class, not a heartbeat. A Diff-Review-sized order
# (~14 KB, #295) dies at one turn (`max turns reached`) because the CLI
# spends turns on read/tool work before the one JSON answer. Sixteen
# covers that cycle; it is not an unbounded subscription loop. The
# workspace-tool vector uses this same default when the node pins no
# `maximum_assistant_turns`.
_HEADLESS_MAXIMUM_TURNS = "16"
_TOOLS_FLAG = "--tools="
_TOOLS_OPTION = "--tools"
_PERMISSION_MODE_FLAG = "--permission-mode"
_DONT_ASK = "dontAsk"
# Measured 01.09.2026 (#642, Debug-Runde 4): grok 1.0.5 asks to confirm a
# fixed Dangerous-command list (rm, chmod, chown, chgrp, chattr, pkill, kill,
# killall, git push) that headless can never answer, and cancels the whole
# session mid-plan instead of refusing the one command. `bypassPermissions`
# is the only measured cure; it does not widen `--tools` or weaken `--deny
# MCPTool`. Operator ruling 01.09.2026: used by the workspace-tool vector
# only, where it runs inside the disposable per-attempt workspace.
_BYPASS_PERMISSIONS = "bypassPermissions"
_ALLOW_FLAG = "--allow"
_DENY_FLAG = "--deny"
# Measured against grok 1.0.4 `--help`: each of these names an ambient surface
# a single headless turn must not reach. They are containment, not preference.
_NO_MEMORY_FLAG = "--no-memory"
_NO_SUBAGENTS_FLAG = "--no-subagents"
_NO_WEB_SEARCH_FLAG = "--disable-web-search"

_INSPECT_COMMAND = "inspect"
_INSPECT_JSON_FLAG = "--json"
_INSPECT_OUTPUT_BYTES = 1_048_576
# `grok inspect --json` names every surface it would load for a directory. A
# discovered plugin, hook, MCP server, skill, marketplace, LSP server,
# permission source or project instruction is trust this seam never granted.
_DISCOVERY_SURFACES = (
    "plugins",
    "hooks",
    "mcpServers",
    "skills",
    "marketplaces",
    "lspServers",
    "projectInstructions",
)
_CONFIG_LAYERS_FIELD = "layers"
_AGENTS_FIELD = "agents"
_AGENT_SOURCE_FIELD = "source"
_AGENT_SOURCE_TYPE_FIELD = "type"
_BUILTIN_AGENT_SOURCE = "builtin"
_PERMISSIONS_FIELD = "permissions"
_PERMISSION_SOURCES_FIELD = "sources"

_EXTERNAL_COMPATIBILITY_CELLS = (
    ("cursor", "skills"),
    ("cursor", "rules"),
    ("cursor", "agents"),
    ("cursor", "mcps"),
    ("cursor", "hooks"),
    ("cursor", "sessions"),
    ("claude", "skills"),
    ("claude", "rules"),
    ("claude", "agents"),
    ("claude", "mcps"),
    ("claude", "hooks"),
    ("claude", "sessions"),
    ("codex", "sessions"),
)
_EXTERNAL_COMPATIBILITY_FIELD = "externalCompat"
_REMOTE_SETTINGS_LOADED_FIELD = "remoteSettingsLoaded"
_COMPATIBILITY_CELLS_FIELD = "cells"
_COMPATIBILITY_VENDOR_FIELD = "vendor"
_COMPATIBILITY_SURFACE_FIELD = "surface"
_COMPATIBILITY_ENABLED_FIELD = "enabled"
_COMPATIBILITY_SOURCE_FIELD = "source"
_CONFIG_SOURCE_ROLE_FIELD = "role"
_CONFIG_SOURCE_PATH_FIELD = "path"
_USER_CONFIG_SOURCE_ROLE = "user"
_CONFIG_SOURCE = "config"

_JOB_DIRECTORY_PREFIX = "atelier2-grok-job-"
_CONFIG_FILE_NAME = "config.toml"
AUTHENTICATION_FILE_NAME = "auth.json"
_JOB_DIRECTORY_MODE = 0o700
_CONFIG_FILE_MODE = 0o600
_AUTHENTICATION_FILE_MODE = 0o400
_PRIVATE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_MAXIMUM_AUTHENTICATION_FILE_BYTES = 1_048_576

_HOME_VARIABLE = "HOME"
_CREDENTIAL_DIRECTORY_VARIABLE = "GROK_HOME"
_SEARCH_PATH_VARIABLE = "PATH"
_SHELL_VARIABLE = "SHELL"
_TERMINAL_TYPE_VARIABLE = "TERM"
# Headless grok 1.0.5 resolves its shell from `$SHELL` (xAI shell_state.rs
# `ShellKind::detect`); under the minimal three-variable environment above, a
# `run_terminal_cmd` that stays silent for ~25s cancels the whole session
# instead of finishing (measured: 5/6 and 9/10 probe runs; see issue #642).
# Naming a concrete shell and terminal type is what keeps that command alive.
# A real PTY is the structural successor to this workaround (issue #943).
_SHELL_VALUE = "/bin/bash"
_TERMINAL_TYPE_VALUE = "xterm-256color"
_TEXT_FIELD = "text"
_STRUCTURED_OUTPUT_FIELD = "structuredOutput"

# The stream vocabulary of `streaming-messages-json`, read off the measured
# capture named at `_STREAMING_MESSAGES_JSON_OUTPUT_FORMAT`. Every line and
# every content block that release writes is named here, so a step a session
# really took is never kept as raw provider output for want of a name.
_LINE_TYPE_FIELD = "type"
_SYSTEM_LINE_TYPE = "system"
_SESSION_INIT_SUBTYPE = "init"
_ASSISTANT_LINE_TYPE = "assistant"
_USER_LINE_TYPE = "user"
_RESULT_LINE_TYPE = "result"
_MESSAGE_FIELD = "message"
_CONTENT_FIELD = "content"
_TEXT_BLOCK_TYPE = "text"
_THINKING_BLOCK_TYPE = "thinking"
_THINKING_FIELD = "thinking"
_TOOL_USE_BLOCK_TYPE = "tool_use"
_TOOL_RESULT_BLOCK_TYPE = "tool_result"
_TOOL_NAME_FIELD = "name"
_TOOL_INPUT_FIELD = "input"
_TOOL_USE_ID_FIELD = "id"
_ANSWERED_TOOL_USE_ID_FIELD = "tool_use_id"
_RESULT_FIELD = "result"
_LINE_SUBTYPE_FIELD = "subtype"
_ERROR_FLAG_FIELD = "is_error"
_USAGE_FIELD = "usage"
_INPUT_TOKENS_FIELD = "input_tokens"
_OUTPUT_TOKENS_FIELD = "output_tokens"
_CACHE_READ_TOKENS_FIELD = "cache_read_input_tokens"
_CACHE_CREATION_TOKENS_FIELD = "cache_creation_input_tokens"
# What this executor keeps of the session header: the session the CLI opened,
# the model that answered it, the doors it granted, and the regime it granted
# them under. The rest of that line -- slash commands, an empty MCP and skill
# inventory, the working directory, a uuid -- is the noise that pushed real
# steps out of the document bound.
_SESSION_HEADER_FIELDS = ("session_id", "model", "tools", "permissionMode")
_RECORD_SEPARATOR = "\n"
_SINGLE_PROMPT_FLAG = "-p"
_MEASURED_INLINE_PROMPT_BYTES = 30_000
_PROMPT_LIMIT_REFUSAL = (
    "Grok 1.0.5 inline prompt transport is measured only through 30,000 bytes"
)
_PROMPT_ENCODING_REFUSAL = "Grok inline prompt transport accepts UTF-8 job bytes only"

# How a declared output schema reaches an operation that may not carry the flag.
# Measured 04.09.2026 on grok 1.0.5 / grok-4.6 (`#1174`) with exactly this
# wording on the workspace-tool vector: the call narrated freely, opened its
# doors, and ended on one bare JSON document as its terminal `result` -- for an
# object schema and for a root-string schema alike.
_OUTPUT_SCHEMA_ASK_HEADING = "--- final answer: declared output schema ---"
_OUTPUT_SCHEMA_ASK = (
    "Your last message is the answer, and nothing else in this session is. "
    "Send it as exactly one JSON document matching this schema, on its own, "
    "with no prose, no explanation and no code fence around it:"
)


def _job_with_output_schema_ask(
    job_bytes: bytes, declared_output_schema_bytes: bytes | None
) -> bytes:
    """The job, closed by the shape its node declared for the answer.

    The schema travels as its exact published document bytes -- the same ones
    the output seam later judges the answer against -- so the sentence the model
    reads and the contract it is held to cannot drift apart. A node that
    declared no schema is left alone: there is nothing to ask for.
    """

    if declared_output_schema_bytes is None:
        return job_bytes
    ask = f"\n\n{_OUTPUT_SCHEMA_ASK_HEADING}\n\n{_OUTPUT_SCHEMA_ASK}\n\n"
    return job_bytes + ask.encode("utf-8") + declared_output_schema_bytes


def _unusable_provider_answer(
    transcript: AttemptTranscript | None,
) -> AgentExecutionFailure:
    """This call produced no answer this executor may use, with what it wrote."""

    return AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY, transcript
    )


class GrokSubscriptionAuthModeUnsupported(ValueError):
    """A published configuration bound a non-subscription profile to this executor."""


class GrokExecutableUnsupported(ValueError):
    """The named executable is not a Grok CLI this executor was measured against."""


class GrokContainmentUnattested(ValueError):
    """The composed profile discovers a surface this executor never granted."""


@dataclass(frozen=True)
class GrokProviderEndedWithoutFinalMessage(AgentExecutionFailure):
    """Grok ended after progress messages without publishing a final envelope."""

    code: AgentAttemptFailureCode = field(
        default=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        init=False,
    )


@dataclass(frozen=True)
class GrokProviderEndedWithoutToolUse(AgentExecutionFailure):
    """Grok ended a workspace-tool session that never opened a single door.

    Not a bad answer -- an answer to a call that never happened. Measured
    04.09.2026 on grok 1.0.5 / grok-4.6 (`#1165`): `--json-schema` constrains
    *every* assistant message rather than the last one, and the CLI ends the
    session at the first message that carries no tool call. The narration a
    model writes before its first tool call is therefore pressed into the
    report form, and where that narration happens to carry no tool call the
    session ends right there -- with a schema-valid report about work nobody
    did. It is a coin flip, not a flag error: the same vector answered after
    three real tool-using turns in the runs beside it.

    A binding that asked for `HEADLESS_WITH_TOOLS` asked for a call that acts
    where it stands, so a session with no tool call is this provider ending
    early, and the attempt says so instead of publishing the preamble as a
    candidate report. It carries the same code as its sibling above because it
    is the same fact about the same call: this process left no answer this
    executor may use. The stream it did write is kept, so a reader sees the
    preamble that ended it.
    """

    code: AgentAttemptFailureCode = field(
        default=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        init=False,
    )


def _parsed_version(reported: str) -> tuple[int, int, int] | None:
    """Read `grok 1.0.5 (...)` as the version."""

    tokens = reported.strip().split()
    if not tokens:
        return None
    leading = (
        tokens[1] if tokens[0].lower() == "grok" and len(tokens) > 1 else tokens[0]
    )
    parts = leading.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def read_grok_version(
    executable: Path, timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Ask one executable which Grok it is. Runs it with `--version`."""

    with tempfile.TemporaryDirectory(prefix="atelier2-grok-version-") as probe_root:
        try:
            process = subprocess.Popen(
                (str(executable), _VERSION_FLAG),
                cwd=probe_root,
                env={},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise GrokExecutableUnsupported(
                f"the Grok executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
        try:
            return_code, answer = bounded_process_answer(
                process, timeout_seconds, _VERSION_PROBE_OUTPUT_BYTES
            )
        except OSError as error:
            raise GrokExecutableUnsupported(
                f"the Grok executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
    if return_code != 0:
        raise GrokExecutableUnsupported(
            f"the Grok executable refused {_VERSION_FLAG} with exit code {return_code}"
        )
    version = _parsed_version(answer.decode("utf-8", "replace"))
    if version is None:
        raise GrokExecutableUnsupported(
            f"the Grok executable did not report a version at {_VERSION_FLAG}"
        )
    return version


def verify_grok_capability(
    executable: Path, timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Refuse an executable outside the reviewed conformance set."""

    version = read_grok_version(executable, timeout_seconds)
    if version not in CONFORMANT_GROK_VERSIONS:
        reported = ".".join(str(part) for part in version)
        conformant = ", ".join(
            ".".join(str(part) for part in candidate)
            for candidate in sorted(CONFORMANT_GROK_VERSIONS)
        )
        raise GrokExecutableUnsupported(
            f"serving Grok subscription agents requires Grok {conformant}, "
            f"not {reported}: this executor's invocation semantics were measured "
            "against that exact release"
        )
    return version


@dataclass(frozen=True)
class GrokSubscriptionSettings:
    executable: Path
    workspace: Path
    credential_directory: Path
    search_path: str

    def __post_init__(self) -> None:
        executable = self.executable.resolve()
        workspace = self.workspace.resolve()
        credential_directory = self.credential_directory.resolve()
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "credential_directory", credential_directory)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the Grok executable must be an executable file")
        if not workspace.is_dir():
            raise ValueError("the Grok workspace must be an existing directory")
        if not credential_directory.is_dir():
            raise ValueError(
                "the Grok credential directory must be an existing directory"
            )
        authentication = credential_directory / AUTHENTICATION_FILE_NAME
        try:
            authentication_status = authentication.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError(
                "the Grok credential directory must contain a regular auth.json"
            ) from error
        if (
            not stat.S_ISREG(authentication_status.st_mode)
            or stat.S_IMODE(authentication_status.st_mode) & 0o077
        ):
            raise ValueError("the Grok auth.json must be a private regular file")
        if not self.search_path.strip():
            raise ValueError("the Grok executable search path must be nonempty")


def _json_schema_flag(declared_output_schema_bytes: bytes | None) -> tuple[str, ...]:
    """The `--json-schema` pair, or nothing where the node declared none.

    These are the exact published document bytes the output seam later
    judges -- not a second serialization. The flag constrains the model and,
    left alone, implies `--output-format json`; a provider that ignores it is
    still refused by the seam. The CLI accepts `{"type":"string"}` and refuses
    a boolean schema (`true`) with "must be a JSON object describing a JSON
    Schema"; this seam does not rewrite that form. A schema-bearing result
    carries its structured answer, which each operation reads from its own
    wire shape and serializes without deciding whether it satisfies the
    contract; the output seam remains the final judge.

    What the flag constrains is every assistant message, not the last one
    (measured 04.09.2026, grok 1.0.5 / grok-4.6, `#1165`), so only the tool-free
    operation carries it: one call, one message, one answer. Its sibling with
    tools has to narrate and act before it answers, and asks for the same shape
    in words instead (`_job_with_output_schema_ask`).
    """
    flag_arguments: list[str] = []
    if declared_output_schema_bytes is not None:
        try:
            schema_text = declared_output_schema_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("declared output schema bytes must be UTF-8") from error
        flag_arguments = [_JSON_SCHEMA_FLAG, schema_text]
    return tuple(flag_arguments)


def _child_environment(
    settings: GrokSubscriptionSettings, state_directory: Path
) -> tuple[tuple[str, str], ...]:
    """The complete environment a launched Grok inherits, and nothing else."""

    # `GROK_HOME` alone does not isolate the CLI. Measured on grok 1.0.4: with
    # `GROK_HOME` pointed at an empty directory and no `HOME` in the child
    # environment, `grok inspect` still discovered 1 plugin, 1 hook, 19 skills,
    # a project instruction, a permission source and ten plugin-sourced agents
    # -- it resolves the invoking account's home and loads that profile. Naming
    # `HOME` as the same private directory is what empties every surface.
    return (
        (_HOME_VARIABLE, str(state_directory)),
        (_CREDENTIAL_DIRECTORY_VARIABLE, str(state_directory)),
        (_SEARCH_PATH_VARIABLE, settings.search_path),
        (_SHELL_VARIABLE, _SHELL_VALUE),
        (_TERMINAL_TYPE_VARIABLE, _TERMINAL_TYPE_VALUE),
    )


def _configuration_bytes() -> bytes:
    sections: list[str] = []
    for vendor in ("cursor", "claude", "codex"):
        lines = [f"[compat.{vendor}]"]
        lines.extend(
            f"{surface} = false"
            for candidate, surface in _EXTERNAL_COMPATIBILITY_CELLS
            if candidate == vendor
        )
        sections.append("\n".join(lines))
    return ("\n\n".join(sections) + "\n").encode("ascii")


def _discovered_surfaces(
    inspected: dict[str, object], state_directory: Path
) -> tuple[str, ...]:
    """Name every surface the reported configuration would load."""

    discovered: list[str] = []
    for surface in _DISCOVERY_SURFACES:
        entries = inspected.get(surface)
        if isinstance(entries, dict):
            entries = entries.get(_CONFIG_LAYERS_FIELD)
        if isinstance(entries, list) and entries:
            discovered.append(f"{surface}={len(entries)}")
    permissions = inspected.get(_PERMISSIONS_FIELD)
    if isinstance(permissions, dict):
        sources = permissions.get(_PERMISSION_SOURCES_FIELD)
        if isinstance(sources, list) and sources:
            discovered.append(f"{_PERMISSIONS_FIELD}.{_PERMISSION_SOURCES_FIELD}")
    config_sources = inspected.get("configSources")
    expected_layers = [
        {
            _CONFIG_SOURCE_ROLE_FIELD: _USER_CONFIG_SOURCE_ROLE,
            _CONFIG_SOURCE_PATH_FIELD: str(state_directory / _CONFIG_FILE_NAME),
        }
    ]
    if (
        not isinstance(config_sources, dict)
        or config_sources.get(_CONFIG_LAYERS_FIELD) != expected_layers
    ):
        discovered.append("configSources")
    compatibility = inspected.get(_EXTERNAL_COMPATIBILITY_FIELD)
    expected_cells = [
        {
            _COMPATIBILITY_VENDOR_FIELD: vendor,
            _COMPATIBILITY_SURFACE_FIELD: surface,
            _COMPATIBILITY_ENABLED_FIELD: False,
            _COMPATIBILITY_SOURCE_FIELD: _CONFIG_SOURCE,
        }
        for vendor, surface in _EXTERNAL_COMPATIBILITY_CELLS
    ]
    if (
        not isinstance(compatibility, dict)
        or compatibility.get(_REMOTE_SETTINGS_LOADED_FIELD) is not False
        or compatibility.get(_COMPATIBILITY_CELLS_FIELD) != expected_cells
    ):
        discovered.append(_EXTERNAL_COMPATIBILITY_FIELD)
    agents = inspected.get(_AGENTS_FIELD)
    if isinstance(agents, list):
        discovered.extend(filter(None, map(_agent_discovery, agents)))
    return tuple(discovered)


def _agent_discovery(agent: object) -> str | None:
    """The surface one reported agent loads; nothing for a built-in one."""
    if not isinstance(agent, dict):
        return _AGENTS_FIELD
    source = agent.get(_AGENT_SOURCE_FIELD)
    kind = source.get(_AGENT_SOURCE_TYPE_FIELD) if isinstance(source, dict) else None
    return None if kind == _BUILTIN_AGENT_SOURCE else f"{_AGENTS_FIELD}:{kind}"


def attest_grok_containment(
    settings: GrokSubscriptionSettings,
    state_directory: Path,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> None:
    """Refuse to serve when the composed profile discovers a trusted surface.

    `--tools=` removes built-ins; it says nothing about the plugins, hooks, MCP
    servers and agent definitions the CLI loads from the workspace and from
    `GROK_HOME`. Trusted hook or MCP code would run with the server's own
    privileges, so this asks the CLI what it would load, with exactly the
    environment and working directory a job would get, and refuses on anything.
    """

    try:
        process = subprocess.Popen(
            (
                str(settings.executable),
                _INSPECT_COMMAND,
                _INSPECT_JSON_FLAG,
            ),
            cwd=state_directory,
            env=dict(_child_environment(settings, state_directory)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise GrokContainmentUnattested(
            f"the Grok executable did not answer {_INSPECT_COMMAND}: {error}"
        ) from error
    try:
        return_code, answer = bounded_process_answer(
            process, timeout_seconds, _INSPECT_OUTPUT_BYTES
        )
    except OSError as error:
        raise GrokContainmentUnattested(
            f"the Grok executable did not answer {_INSPECT_COMMAND}: {error}"
        ) from error
    if return_code != 0:
        raise GrokContainmentUnattested(
            f"the Grok executable refused {_INSPECT_COMMAND} with exit code "
            f"{return_code}: the served profile is unattested"
        )
    try:
        inspected: object = json.loads(answer)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GrokContainmentUnattested(
            f"the Grok executable did not report {_INSPECT_COMMAND} as JSON"
        ) from error
    if not isinstance(inspected, dict):
        raise GrokContainmentUnattested(
            f"the Grok executable did not report {_INSPECT_COMMAND} as an object"
        )
    discovered = _discovered_surfaces(inspected, state_directory)
    if discovered:
        raise GrokContainmentUnattested(
            "serving Grok subscription agents requires a profile that discovers "
            "no plugin, hook, MCP server, skill, marketplace, LSP server, "
            "permission source, project instruction, external compatibility cell "
            "or non-built-in agent; this exact invocation discovers "
            f"{', '.join(discovered)}"
        )


def _write_private_file(path: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(path, _PRIVATE_FILE_FLAGS, mode)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("private Grok file write made no progress")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def _authentication_bytes(settings: GrokSubscriptionSettings) -> bytes:
    path = settings.credential_directory / AUTHENTICATION_FILE_NAME
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o077:
            raise ValueError("the Grok auth.json must be a private regular file")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > _MAXIMUM_AUTHENTICATION_FILE_BYTES:
                raise ValueError("the Grok auth.json exceeds its private copy bound")
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _open_job_directory(settings: GrokSubscriptionSettings) -> Path:
    """Prepare one private, disposable Grok home."""

    directory = Path(
        tempfile.mkdtemp(prefix=_JOB_DIRECTORY_PREFIX, dir=settings.workspace)
    )
    os.chmod(directory, _JOB_DIRECTORY_MODE)
    prepared = False
    try:
        _write_private_file(
            directory / AUTHENTICATION_FILE_NAME,
            _authentication_bytes(settings),
            _AUTHENTICATION_FILE_MODE,
        )
        _write_private_file(
            directory / _CONFIG_FILE_NAME,
            _configuration_bytes(),
            _CONFIG_FILE_MODE,
        )
        prepared = True
        return directory
    finally:
        if not prepared:
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass


def _validated_inline_prompt(job_bytes: bytes) -> str:
    """Return a measured Grok prompt or refuse before creating a provider command."""

    if len(job_bytes) > _MEASURED_INLINE_PROMPT_BYTES:
        raise AgentExecutionPreflightRefusal(_PROMPT_LIMIT_REFUSAL)
    try:
        return job_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AgentExecutionPreflightRefusal(_PROMPT_ENCODING_REFUSAL) from error


def _headless_arguments(
    executable: Path,
    model: str,
    prompt: str,
    declared_output_schema_bytes: bytes | None,
) -> tuple[str, ...]:
    """The exact argument vector one tool-free invocation is launched with.

    The grok 1.0.5 transport measurement above established `-p` as the only
    carrier that echoed every deep token through the 30,000-byte bound. The
    prompt has already passed this carrier's UTF-8 and measured-size boundary.
    """

    return (
        str(executable),
        _OUTPUT_FORMAT_FLAG,
        _JSON_OUTPUT_FORMAT,
        *_json_schema_flag(declared_output_schema_bytes),
        _MODEL_FLAG,
        model,
        _SINGLE_PROMPT_FLAG,
        prompt,
        _TOOLS_FLAG,
        # No tool is granted here, so no Dangerous-list confirmation can ever
        # arise: `dontAsk` has nothing to ask about, and stays.
        _PERMISSION_MODE_FLAG,
        _DONT_ASK,
        _NO_MEMORY_FLAG,
        _NO_SUBAGENTS_FLAG,
        _NO_WEB_SEARCH_FLAG,
        _MAXIMUM_TURNS_FLAG,
        _HEADLESS_MAXIMUM_TURNS,
    )


def _json_values(standard_output: bytes) -> tuple[object, ...] | None:
    """Read every JSON value the tool-free `--output-format json` call wrote.

    Grok puts no separator between JSON values, so `raw_decode` is the framing
    owner here instead of treating a whole frame as one JSON instance: it
    returns the first value and the character where the next one begins.
    Measured 04.09.2026 on grok 1.0.5 / grok-4.6, this format writes exactly
    one value even across several model calls, so a second value is the shape
    this reader refuses to mistake for one. Invalid UTF-8 and an unreadable
    value leave the raw frame for the
    transcript rather than inventing a partial answer.
    """

    try:
        source = standard_output.decode("utf-8")
    except UnicodeDecodeError:
        return None
    decoder = json.JSONDecoder()
    values: list[object] = []
    index = 0
    while index < len(source):
        while index < len(source) and source[index] in " \t\r\n":
            index += 1
        if index == len(source):
            break
        try:
            value, index = decoder.raw_decode(source, index)
        except (ValueError, RecursionError):
            return None
        values.append(value)
    return tuple(values)


def _canonical_json(value: object) -> str:
    """One decoded provider value in the transcript's readable representation."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _grok_value_step(value: object) -> TranscriptEvent:
    """Keep narration as speech and every other intermediate shape as evidence."""

    if isinstance(value, str):
        return AssistantTurn(value)
    return UnrecognisedProviderOutput(_canonical_json(value))


def _grok_transcript(values: Sequence[object]) -> AttemptTranscript | None:
    """What Grok published before an answer this adapter may accept, if any."""

    return (
        AttemptTranscript.of(_grok_value_step(value) for value in values)
        if values
        else None
    )


def _tool_free_envelope_text(values: Sequence[object]) -> str | None:
    """The tool-free call's answer: the `text` of the one envelope it wrote.

    Only the tool-free operation reads this. Its sibling with tools writes an
    NDJSON stream whose terminal line names the answer in its own field, so
    that operation never has to read one envelope's `text` -- which for a
    tool-using call is the concatenation of every schema-shaped message the
    session produced rather than its answer (`#1165`).
    """

    final_value = values[-1]
    if not isinstance(final_value, dict):
        return None
    text = final_value.get(_TEXT_FIELD)
    return text if isinstance(text, str) and text else None


def _unreadable_grok_transcript(standard_output: bytes) -> AttemptTranscript | None:
    """Keep an unreadable raw frame as bounded, redacted evidence."""

    if not standard_output:
        return None
    return AttemptTranscript.of(
        [UnrecognisedProviderOutput(standard_output.decode("utf-8", "replace"))]
    )


def _stream_lines(standard_output: bytes) -> tuple[str, ...]:
    """Every line of this stream that carries anything, as text.

    Split on the line feed alone, because that is the record separator NDJSON
    names: `splitlines` also cuts at U+2028, U+2029, form feed and carriage
    return, every one of which a JSON string may legally contain, so one model
    quoting a line separator would have its own answer torn into halves that
    parse as neither. Decoding replaces what is not UTF-8 rather than refusing
    the frame: the bytes a failing process wrote are the diagnosis somebody is
    looking for, and what survives is still bounded and redacted by the
    transcript contract before it is kept.
    """

    return tuple(
        line
        for line in standard_output.decode("utf-8", "replace").split(_RECORD_SEPARATOR)
        if line.strip()
    )


def _stream_entry(line: str) -> dict[str, object] | None:
    """This line read as one stream entry, or nothing where it is not one.

    Every way the parser can refuse a line is contained, not bad syntax alone:
    a JSON integer of more than a few thousand digits raises a plain
    `ValueError` from the interpreter's own conversion limit, and a deeply
    nested document raises `RecursionError`. Either one escaping here would end
    the attempt on an exception nobody stored, over a line this executor was in
    any case only ever going to keep as text.
    """

    try:
        entry: object = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return entry if isinstance(entry, dict) else None


def _message_content_blocks(entry: dict[str, object]) -> tuple[object, ...]:
    """The content blocks this message line carries, or none."""

    message = entry.get(_MESSAGE_FIELD)
    if not isinstance(message, dict):
        return ()
    content = message.get(_CONTENT_FIELD)
    return tuple(content) if isinstance(content, list) else ()


def _block_step(block: object, tool_names: dict[str, str]) -> TranscriptEvent:
    """One content block as the step it is, or as the output it stays.

    Every block yields something. A shape this executor does not name -- a kind
    a release adds tomorrow -- is kept as the provider's own output rather than
    dropped, because a line that IS recognised is exactly where a dropped block
    hides best.

    A thinking block is one of the agent's turns and is kept as one: it is what
    the agent said to itself on the way to its answer, and reading it is how a
    person tells a lucky answer from a reasoned one. Its `signature` is not
    kept. That blob is the provider's attestation for handing the block back to
    its own API, which this transcript never does; measured 04.09.2026 it is
    several hundred base64 characters on every assistant message, and keeping
    the raw block was pushing real steps out of the document bound (`#1174`).

    `tool_names` is how a result finds the door it answered: the provider names
    the tool on the call and refers back to it by id afterwards, so the call
    leaves its name here for the result to read. A result whose call this
    executor never saw keeps the id instead of borrowing another tool's name.
    """

    if not isinstance(block, dict):
        return UnrecognisedProviderOutput(_canonical_json(block))
    shape = block.get(_LINE_TYPE_FIELD)
    step: TranscriptEvent | None = None
    if shape == _TEXT_BLOCK_TYPE:
        spoken = block.get(_TEXT_FIELD)
        step = AssistantTurn(spoken) if isinstance(spoken, str) else None
    elif shape == _THINKING_BLOCK_TYPE:
        thought = block.get(_THINKING_FIELD)
        step = AssistantTurn(thought) if isinstance(thought, str) else None
    elif shape == _TOOL_USE_BLOCK_TYPE:
        name = block.get(_TOOL_NAME_FIELD)
        if isinstance(name, str):
            called_id = block.get(_TOOL_USE_ID_FIELD)
            if isinstance(called_id, str):
                tool_names[called_id] = name
            step = ToolCalled(name, _canonical_json(block.get(_TOOL_INPUT_FIELD)))
    elif shape == _TOOL_RESULT_BLOCK_TYPE:
        step = _reply(block, tool_names)
    return UnrecognisedProviderOutput(_canonical_json(block)) if step is None else step


def _reply(block: dict[str, object], tool_names: dict[str, str]) -> ToolReturned | None:
    answered_id = block.get(_ANSWERED_TOOL_USE_ID_FIELD)
    if not isinstance(answered_id, str):
        return None
    content = block.get(_CONTENT_FIELD)
    shown = content if isinstance(content, str) else _canonical_json(content)
    return ToolReturned(tool_names.get(answered_id, answered_id), shown)


def _session_header_step(entry: dict[str, object]) -> UnrecognisedProviderOutput:
    """This stream's opening `system` line, reduced to the facts it names.

    The line is recognised, so nothing of it is guessed at; only what it says
    about the session survives (`_SESSION_HEADER_FIELDS`). A release that
    renames every one of those fields leaves the whole line standing instead of
    an empty mapping -- the same discipline `_block_step` keeps, because a line
    this reader DOES recognise is exactly where a lost line hides best.

    It stays the provider's own output because the transcript vocabulary has no
    step for a session header, and giving it one is a contract change across the
    wire, the projection and the run-log surface rather than an adapter's
    decision -- named as deferred to the shared stream reader (`#892`).
    """

    named = {name: entry[name] for name in _SESSION_HEADER_FIELDS if name in entry}
    return UnrecognisedProviderOutput(_canonical_json(named or entry))


def _token_count(usage: dict[str, object], field_name: str) -> int | None:
    """One nonnegative token count this usage record states, or nothing."""

    counted = usage.get(field_name, 0)
    if type(counted) is not int or counted < 0:
        return None
    return counted


def _usage_step(entry: dict[str, object]) -> Usage | None:
    """What this terminal line says the whole call spent, where it says it whole.

    The terminal line is the one this is read from: it carries the provider's
    own total, while the per-message records would have to be added up here and
    this module would then be the second place deciding what an attempt cost.
    """

    usage = entry.get(_USAGE_FIELD)
    if not isinstance(usage, dict):
        return None
    counts = tuple(
        _token_count(usage, field_name)
        for field_name in (
            _INPUT_TOKENS_FIELD,
            _OUTPUT_TOKENS_FIELD,
            _CACHE_READ_TOKENS_FIELD,
            _CACHE_CREATION_TOKENS_FIELD,
        )
    )
    if any(count is None for count in counts):
        return None
    read, written, cached, created = counts
    return Usage(read or 0, written or 0, cached or 0, created or 0)


def _terminal_refusal_step(entry: dict[str, object]) -> ProviderTerminalRefusal | None:
    """The provider's own ending, where this terminal line declared an error.

    Only an explicit `true` counts: a line that never mentions the flag, or
    spells it as anything other than the JSON boolean, is the success path this
    vocabulary already knew. Once the flag is true the step is always kept, even
    where the line names nothing else -- a call whose usage record parses would
    otherwise leave the transcript saying what it spent and never why it ended.

    Two fields carry that why, and they are the ones grok's terminal line was
    measured to write (04.09.2026, grok 1.0.5 / grok-4.6, `#1165`): `subtype`,
    which names the ending the CLI gave itself, and `result`, its own last
    words. Claude's twin reads an `api_error_status` beside them; grok writes no
    such field, so this step keeps it as the empty text the contract reserves
    for a provider release that does not carry it.
    """

    if entry.get(_ERROR_FLAG_FIELD) is not True:
        return None
    ending = entry.get(_LINE_SUBTYPE_FIELD)
    last_words = entry.get(_RESULT_FIELD)
    return ProviderTerminalRefusal(
        ending if isinstance(ending, str) else "",
        "",
        last_words if isinstance(last_words, str) else "",
    )


def _line_steps(line: str, tool_names: dict[str, str]) -> tuple[TranscriptEvent, ...]:
    """The steps one stream line stands for, keeping whole what it does not.

    A line this executor cannot read as steps is kept as the provider's own
    output rather than dropped: the session header, a line shape a release
    added, and whatever a call that never produced a stream printed instead all
    arrive here, and each is evidence about an episode somebody is reading
    precisely because nothing explained it.
    """

    entry = _stream_entry(line)
    if entry is None:
        return (UnrecognisedProviderOutput(line),)
    shape = entry.get(_LINE_TYPE_FIELD)
    steps: tuple[TranscriptEvent, ...] = ()
    if (
        shape == _SYSTEM_LINE_TYPE
        and entry.get(_LINE_SUBTYPE_FIELD) == _SESSION_INIT_SUBTYPE
    ):
        steps = (_session_header_step(entry),)
    elif shape in {_ASSISTANT_LINE_TYPE, _USER_LINE_TYPE}:
        steps = tuple(
            _block_step(block, tool_names) for block in _message_content_blocks(entry)
        )
    elif shape == _RESULT_LINE_TYPE:
        refused = _terminal_refusal_step(entry)
        spent = _usage_step(entry)
        steps = tuple(step for step in (refused, spent) if step is not None)
    return steps if steps else (UnrecognisedProviderOutput(line),)


@dataclass(frozen=True)
class GrokStreamedSession:
    """One workspace-tool call read back from the stream it wrote.

    `terminal_envelope` is the stream's own last word rather than a guess: the
    CLI names its terminal line, so a call that died mid-stream has none here
    instead of having its last progress line read as an answer.

    `opened_a_door` is read from the steps before they become a transcript,
    because a transcript drops its oldest steps to stay within the document
    bound -- asking the kept steps whether a tool ran would answer "no" for
    exactly the long sessions that used the most tools.
    """

    steps: tuple[TranscriptEvent, ...]
    terminal_envelope: dict[str, object] | None
    opened_a_door: bool

    @property
    def transcript(self) -> AttemptTranscript | None:
        """What this call did, or an honest nothing where it wrote no line."""

        return AttemptTranscript.of(self.steps) if self.steps else None


def _streamed_session(standard_output: bytes) -> GrokStreamedSession:
    """Read one whole `--output-format streaming-messages-json` call back."""

    lines = _stream_lines(standard_output)
    tool_names: dict[str, str] = {}
    steps = tuple(step for line in lines for step in _line_steps(line, tool_names))
    terminal = _stream_entry(lines[-1]) if lines else None
    if terminal is not None and terminal.get(_LINE_TYPE_FIELD) != _RESULT_LINE_TYPE:
        terminal = None
    return GrokStreamedSession(
        steps, terminal, any(isinstance(step, ToolCalled) for step in steps)
    )


@dataclass(frozen=True)
class GrokSubscriptionProcessCommand(AgentProcessCommand):
    """One Grok headless command and the output schema its node declared.

    The tool-free vector is the one that carries those bytes in argv, as
    `--json-schema`, and reads them back here to know which envelope field its
    answer stands in. The workspace-tool vector carries the same bytes inside
    its prompt instead, so on its command they state what was declared, never
    what stands in its arguments.
    """

    declared_output_schema_bytes: bytes | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class GrokSubscriptionExecutor(PrintModeExecutor):
    settings: GrokSubscriptionSettings
    _invocation_directories: set[Path] = field(
        default_factory=set, init=False, compare=False, repr=False
    )
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, compare=False, repr=False
    )
    _closed: threading.Event = field(
        default_factory=threading.Event, init=False, compare=False, repr=False
    )

    def _invocation_prompt(self, request: AgentExecutionRequestV2) -> str:
        """The exact text one invocation of this operation carries.

        The tool-free call carries the job and nothing else: its `--json-schema`
        flag states the shape of the one answer it is allowed to give.
        """

        return _validated_inline_prompt(request.job_bytes)

    def _invocation_arguments(
        self,
        model: str,
        prompt: str,
        declared_output_schema_bytes: bytes | None,
        maximum_assistant_turns: int | None = None,
    ) -> tuple[str, ...]:
        del maximum_assistant_turns
        return _headless_arguments(
            self.settings.executable,
            model,
            prompt,
            declared_output_schema_bytes,
        )

    _unsupported_auth_message = (
        "the Grok subscription executor serves subscription profiles only"
    )

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise GrokSubscriptionAuthModeUnsupported(self._unsupported_auth_message)
        prompt = self._invocation_prompt(request)
        settings = self.settings
        state_directory = _open_job_directory(settings)
        registered = False
        try:
            attest_grok_containment(settings, state_directory)
            command = GrokSubscriptionProcessCommand(
                self._invocation_arguments(
                    binding.configuration.model,
                    prompt,
                    request.declared_output_schema_bytes,
                    request.maximum_assistant_turns,
                ),
                _child_environment(settings, state_directory),
                b"",
                standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
                declared_output_schema_bytes=request.declared_output_schema_bytes,
            )
            with self._lifecycle_lock:
                if self._closed.is_set():
                    raise RuntimeError("the Grok executor is closed")
                self._invocation_directories.add(state_directory)
                registered = True
            return command
        finally:
            if not registered:
                try:
                    shutil.rmtree(state_directory)
                except FileNotFoundError:
                    pass

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        command = invocation.command
        values = _json_values(completion.standard_output)
        if values is None:
            return _unusable_provider_answer(
                _unreadable_grok_transcript(completion.standard_output)
            )
        if completion.return_code != 0:
            return _unusable_provider_answer(_grok_transcript(values))
        if not values:
            return GrokProviderEndedWithoutFinalMessage()
        # Last value wins: only the final JSON value can be the envelope, and
        # measured 04.09.2026 on grok 1.0.5 / grok-4.6 this format writes
        # exactly one -- a call that wrote none ended without a final message.
        # `text` is the answer and `thought` is narration; `--json-schema` adds
        # `structuredOutput` as the parsed form of `text`, not a later value.
        schema_bearing = (
            isinstance(command, GrokSubscriptionProcessCommand)
            and command.declared_output_schema_bytes is not None
        )
        final_value = values[-1]
        if schema_bearing:
            # Measured on grok 1.0.5: a run that ends without structured
            # output keeps the field as `"structuredOutput": null` beside
            # `structuredOutputError` -- null is the CLI's own no-answer
            # sentinel, never a model answer. Reading it as the JSON value
            # `null` handed the output seam a fabricated `null` answer and
            # dropped this envelope from the evidence (live run
            # 91c76c25, both attempts), so absent and null decode alike:
            # the provider ended without a final message, envelope kept.
            if (
                not isinstance(final_value, dict)
                or final_value.get(_STRUCTURED_OUTPUT_FIELD) is None
            ):
                return GrokProviderEndedWithoutFinalMessage(_grok_transcript(values))
            output_bytes = json.dumps(
                final_value[_STRUCTURED_OUTPUT_FIELD],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            text = _tool_free_envelope_text(values)
            if text is None:
                return GrokProviderEndedWithoutFinalMessage(_grok_transcript(values))
            output_bytes = text.encode("utf-8")
        if len(output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _unusable_provider_answer(_grok_transcript(values))
        return AgentExecutionResult(output_bytes, _grok_transcript(values[:-1]))

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Take back the private home this invocation handed the provider.

        The directory holds a copy of the operator's own `auth.json`, so it is
        taken back on every path rather than with the attempt's workspace: the
        workspace keeps what a provider left behind until the attempt is
        durably terminal, and a credential must not wait that long.
        """

        environment = dict(command.environment)
        home = environment.get(_HOME_VARIABLE)
        grok_home = environment.get(_CREDENTIAL_DIRECTORY_VARIABLE)
        if home is None or home != grok_home:
            raise ValueError("Grok invocation state binding is missing")
        directory = Path(home)
        with self._lifecycle_lock:
            if directory not in self._invocation_directories:
                return
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass
            self._invocation_directories.remove(directory)

    def close(self) -> None:
        with self._lifecycle_lock:
            self._closed.set()
            directories = tuple(self._invocation_directories)
            errors: list[Exception] = []
            for directory in directories:
                try:
                    shutil.rmtree(directory)
                except FileNotFoundError:
                    self._invocation_directories.remove(directory)
                except OSError as error:
                    errors.append(error)
                else:
                    self._invocation_directories.remove(directory)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Grok invocation cleanup failed", errors)


@dataclass(frozen=True)
class GrokSubscriptionExecutorFactory:
    settings: GrokSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return GROK_SUBSCRIPTION_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return frozenset({AgentExecutionCapability.HEADLESS})

    def open(self) -> GrokSubscriptionExecutor:
        return GrokSubscriptionExecutor(self.settings)


GROK_WORKSPACE_TOOLS_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("xai"), AgentExecutorRevision("grok-subscription-tools/v1")
)
# A second operation of the same CLI, not a later revision of the first. Its
# argument vector differs from the tool-free one in the tool grant, and that
# decision is what an operational identity stands for, so every durable
# attempt record keeps saying which of the two ran.
# V3 was the wire change: the vector asks for `--output-format
# streaming-messages-json` instead of `json`, so the process publishes every
# turn and tool call on its way to its answer, and a session that opened no
# door at all is refused instead of published (`#1165`). V4 drops
# `--json-schema` from that vector and asks for the declared shape in the job's
# last words instead (`#1174`), which changes what the model is constrained to
# say on every turn and where the answer is read from. A different vector
# reaching a different wire shape is a different operation, so no attempt
# recorded under an earlier identity is read as having run it.
GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-workspace-tools-streaming-messages-json-schema-in-job/v4"
)

# Headless user-guide names `run_terminal_cmd`; Getting Started names
# `run_terminal_command`. Measured on grok 1.0.4: both parse, because the CLI
# does not check tool IDs at parse time. This executor is a headless
# operation, so it offers the Headless-documented ID. That the model then
# actually calls this ID is the billed secret-file probe after landing, not a
# parse proof.
WORKSPACE_TOOLS = (
    "read_file",
    "list_dir",
    "grep",
    "search_replace",
    "run_terminal_cmd",
)
_WORKSPACE_TOOL_LIST = ",".join(WORKSPACE_TOOLS)
# Permission classes, not tool IDs. `--allow` is repeatable; `--allowedTools`
# is the same flag. These five classes plus `--tools` above are the whole
# grant: only those rules plus built-in read-only, no silent all-tools.
#
# The mode below them is `bypassPermissions`, not `dontAsk`. `dontAsk` was
# headless fiction from the start -- nobody is present to answer a prompt --
# and measurement (#642, 01.09.2026) found the cost of pretending otherwise:
# grok 1.0.5 still confirms a fixed Dangerous-command list (rm, chmod, chown,
# chgrp, chattr, pkill, kill, killall, git push) against a headless caller
# that can never reply, and cancels the *whole session* mid-plan rather than
# refusing the one command. Specific `--allow` globs for those exact classes
# (`Bash(rm *)`, `Bash(git push *)`) are documented by xAI as the escape but
# measured ineffective: the session still cancels. `bypassPermissions` is the
# only vector that healed it end to end, including a real commit and push.
# `bypassPermissions` is a general always-approve, not a Dangerous-list
# exemption (independent codex review, 01.09.2026): it also lifts grok's
# protected-edit confirmation floors on `.git/hooks`, `~/.ssh`, shell startup
# files, `/etc`, grok/Claude/Cursor configuration -- floors that under
# `dontAsk` held by accident, because the unanswerable prompt cancelled the
# session. `WORKSPACE_DENY_RULES` below restores those floors as hard denies:
# denies are evaluated before the catch-all allow bypass appends, so the nine
# Dangerous commands run as intended while the protected-edit surfaces stay
# refused.
WORKSPACE_ALLOW_RULES = ("Read", "Edit", "Write", "Grep", "Bash")
# Docs: MCP meta-tools stay visible unless denied. `--deny MCPTool` parses
# and stays effective under `bypassPermissions`. Combined with private HOME
# and the inert compat config, that is the MCP containment; the executor
# does not claim OS isolation.
_MCP_TOOL_DENY_RULE = "MCPTool"
# The protected-edit surfaces grok's own classifier confirms before an edit
# (its `strings` name `.git/hooks`, `.ssh`, shell startup files, `/etc`, grok
# config, grok sandbox config, Claude-compatible settings, Cursor hooks) --
# the floors `bypassPermissions` would otherwise silently approve. Pattern
# semantics are the CLI's own rule reference: `**` crosses `/` and an
# unrooted leading `**/` matches at any depth, which covers both an absolute
# tool path and the literal `~/`-prefixed spelling the permission check sees
# before tool-side expansion; a leading `~/` in a *pattern* would be literal
# glob text and match nothing. Measured 01.09.2026 against grok 1.0.5 /
# grok-4.6 under `--permission-mode bypassPermissions`: `search_replace` and
# a shell retry against `~/.bashrc`, `scratch/.git/hooks/pre-commit`, a new
# `.git/hooks/pre-push` and a new `~/.ssh/config` were each refused with
# `Denied by permission policy: deny rule on edit matching ...` and left the
# files byte-identical, while a plain write in the working directory
# succeeded. The `Edit` denies carried every refusal (they also govern paths
# shell commands touch); the `Write` denies state the same floor for the
# write rule class the CLI recognizes beside it.
_PROTECTED_EDIT_PATH_PATTERNS = (
    "**/.git/hooks/**",
    "**/.ssh/**",
    "**/.bashrc",
    "**/.bash_profile",
    "**/.bash_login",
    "**/.bash_logout",
    "**/.profile",
    "**/.zshrc",
    "**/.zprofile",
    "**/.zshenv",
    "**/.zlogin",
    "**/.zlogout",
    "**/.cshrc",
    "**/.tcshrc",
    "**/.kshrc",
    "**/.config/fish/**",
    "/etc/**",
    "**/.grok/**",
    "**/.claude/**",
    "**/.cursor/**",
)
WORKSPACE_DENY_RULES = (
    _MCP_TOOL_DENY_RULE,
    *(
        f"{rule_class}({pattern})"
        for rule_class in ("Edit", "Write")
        for pattern in _PROTECTED_EDIT_PATH_PATTERNS
    ),
)

# What the CLI says when it could not read an argument, measured on grok
# 1.0.4 against a flag no release can know. A Clap refusal without an
# isolated HOME can exit 0, so return code 0 is not a parse proof. The probe
# runs in the same private HOME/GROK_HOME a job would get. The marker is
# `unexpected argument`, not Claude's `unknown option`.
_ARGUMENT_REFUSAL_MARKER = "unexpected argument"
_UNKNOWN_FLAG_CONTROL = "--atelier2-no-grok-knows-this"
# The probe never reaches a model, so this names none: it keeps the vector's
# shape exact while saying plainly that no billed call stands behind it.
_INVOCATION_PROBE_MODEL = "atelier2-invocation-probe"
_INVOCATION_PROBE_OUTPUT_BYTES = 16_384
_INVOCATION_PROBE_PREFIX = "atelier2-grok-invocation-"


def _workspace_tool_arguments(
    executable: Path,
    model: str,
    prompt: str,
    maximum_assistant_turns: int | None = None,
) -> tuple[str, ...]:
    """The exact argument vector one workspace-tool invocation is launched with.

    No `--json-schema`: this operation has to narrate and act before it answers,
    and the flag constrains every assistant message rather than the last one, so
    the CLI ends such a session at the first schema-shaped message that carries
    no tool call (`#1165`). The declared schema is asked for in the job instead
    (`_job_with_output_schema_ask`) and judged at the output seam, which was
    always its last instance.
    """

    allow: list[str] = []
    for rule in WORKSPACE_ALLOW_RULES:
        allow.extend((_ALLOW_FLAG, rule))
    deny: list[str] = []
    for rule in WORKSPACE_DENY_RULES:
        deny.extend((_DENY_FLAG, rule))
    return (
        str(executable),
        _OUTPUT_FORMAT_FLAG,
        _STREAMING_MESSAGES_JSON_OUTPUT_FORMAT,
        _MODEL_FLAG,
        model,
        _SINGLE_PROMPT_FLAG,
        prompt,
        _TOOLS_OPTION,
        _WORKSPACE_TOOL_LIST,
        *allow,
        *deny,
        _PERMISSION_MODE_FLAG,
        _BYPASS_PERMISSIONS,
        _NO_MEMORY_FLAG,
        _NO_SUBAGENTS_FLAG,
        _NO_WEB_SEARCH_FLAG,
        _MAXIMUM_TURNS_FLAG,
        (
            str(maximum_assistant_turns)
            if maximum_assistant_turns is not None
            else _HEADLESS_MAXIMUM_TURNS
        ),
    )


def _jobless_invocation_answer(
    settings: GrokSubscriptionSettings,
    arguments: tuple[str, ...],
    state_directory: Path,
    timeout_seconds: float,
) -> str:
    """Start this exact invocation with no credentials, and read back how it refused.

    Both streams are read, because what has to be told apart here is only said
    on the diagnostic one. The call is handed the deployment's search path and
    a private HOME/GROK_HOME -- the launch environment a job would get --
    because a Clap refusal without that isolation can exit 0. Auth is not
    copied: a prompt file with credentials would be a billed call.
    """

    try:
        process = subprocess.Popen(
            arguments,
            cwd=state_directory,
            env=dict(_child_environment(settings, state_directory)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise GrokExecutableUnsupported(
            f"the Grok executable could not start this executor's invocation: {error}"
        ) from error
    try:
        return_code, answer, diagnostics = bounded_process_streams(
            process, timeout_seconds, _INVOCATION_PROBE_OUTPUT_BYTES
        )
    except OSError as error:
        raise GrokExecutableUnsupported(
            f"the Grok executable could not start this executor's invocation: {error}"
        ) from error
    if return_code == 0:
        raise GrokExecutableUnsupported(
            "the Grok executable answered a jobless invocation successfully: "
            "this probe rests on a call with no credentials ending in a "
            "refusal, so a release that runs one instead has to be measured "
            "again before this executor may be composed against it"
        )
    return (answer + diagnostics).decode("utf-8", "replace")


def attest_grok_workspace_tool_invocation(
    settings: GrokSubscriptionSettings,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> None:
    """Refuse an executable that cannot start this executor's exact invocation.

    A version answer is not startability. So this launches the argument vector
    the workspace-tool executor really prepares -- every flag, a private HOME
    -- and hands it no credentials: a CLI that read the whole vector reaches
    its own unsigned-in refusal, and a CLI that did not stops at the argument
    it could not read. Neither reaches a model, so the attestation is free
    and runs at every composition rather than at the first run that binds a
    node.

    The negative observation only means something if this executable can still
    make the positive one, so the control runs beside it: the same vector with
    one flag no release can know must be refused as an unexpected argument.
    Without that control, "said nothing about an unexpected argument" would
    also be what a release that stopped saying it looks like.
    """

    state_directory = Path(
        tempfile.mkdtemp(prefix=_INVOCATION_PROBE_PREFIX, dir=settings.workspace)
    )
    try:
        os.chmod(state_directory, _JOB_DIRECTORY_MODE)
        _write_private_file(
            state_directory / _CONFIG_FILE_NAME,
            _configuration_bytes(),
            _CONFIG_FILE_MODE,
        )
        arguments = _workspace_tool_arguments(
            settings.executable, _INVOCATION_PROBE_MODEL, ""
        )
        started = _jobless_invocation_answer(
            settings, arguments, state_directory, timeout_seconds
        )
        if _ARGUMENT_REFUSAL_MARKER in started:
            raise GrokExecutableUnsupported(
                "the Grok executable refused an argument of this executor's "
                f"invocation: {started.strip()}. Serving workspace-tool agents "
                "needs every flag of that vector to exist and parse, because "
                "each one is a containment decision this executor states"
            )
        control = _jobless_invocation_answer(
            settings,
            (*arguments, _UNKNOWN_FLAG_CONTROL),
            state_directory,
            timeout_seconds,
        )
        if _ARGUMENT_REFUSAL_MARKER not in control:
            raise GrokExecutableUnsupported(
                "the Grok executable did not refuse a flag no release can "
                f"know, answering instead: {control.strip()}. The probe above "
                "reads a missing flag out of exactly that refusal, so an "
                "executable that never states one cannot be attested by it"
            )
    finally:
        try:
            shutil.rmtree(state_directory)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class GrokWorkspaceToolExecutor(GrokSubscriptionExecutor):
    """One headless `grok` call that may use tools where it stands.

    This is the tool-free executor's sibling and deliberately not its successor.
    The two differ in the tool grant, and a node keeps the tool-free one unless
    its durable binding asks for `HEADLESS_WITH_TOOLS`; the capability the
    factory below declares is what makes that ask the only way to reach this
    class.

    WHAT IT GRANTS. Grok splits the two switches Claude combines: `--tools`
    names the built-in IDs the model may see, and `--allow` names the five
    permission classes it may run, under `--permission-mode bypassPermissions`
    (Operator ruling 01.09.2026, #642 -- see `WORKSPACE_ALLOW_RULES` for the
    measurement: `dontAsk` cancelled the whole session on grok's own
    Dangerous-command confirmation, and documented `--allow` globs for those
    commands did not heal it). `--deny` carries `WORKSPACE_DENY_RULES`:
    `MCPTool` keeps MCP meta-tools from remaining visible, and the
    protected-edit path denies restore the confirmation floors bypass would
    otherwise silently approve (see `_PROTECTED_EDIT_PATH_PATTERNS` for that
    measurement). All are measured, not chosen: parse does not check tool
    IDs, so this executor does not pretend it validates them.

    WHAT IT KEEPS. Every other flag and the whole private HOME of the
    tool-free call, unchanged: inline `-p`, the inert compatibility
    configuration, no memory, no subagents, no web search, a bounded turn
    count.

    WHAT IT ASKS FOR. Its answer's shape, in words rather than through
    `--json-schema`: the flag constrains every assistant message, and this
    operation has to narrate and act before it answers, so the flag ended whole
    sessions on their own preamble (`#1165`). The node's declared schema closes
    the job instead (`_job_with_output_schema_ask`), and the output seam judges
    the answer against it exactly as it always did -- an answer that is no such
    document is refused there, typed and retryable, rather than accepted here.

    WHAT IT READS. Not the tool-free call's one envelope. This vector asks for
    `--output-format streaming-messages-json`, so the call publishes its turns
    and its tool calls as NDJSON and names its own terminal line
    (`_streamed_session`), whose nonempty `result` is the answer. A session
    whose stream shows no tool call at all is refused as
    `GrokProviderEndedWithoutToolUse` rather than answered -- see that class
    for the measurement, and `#1165` for the live pass that found it.

    WHAT IT DOES NOT CLAIM. No operating-system isolation. The process runs as
    the serving user, and the named tools reach every path that user reaches
    -- including the credential directory this invocation hands it. The
    attempt's workspace is where the process is *started*, not a boundary it
    is held inside. `bypassPermissions` does not widen `--tools`: a tool this
    vector did not name still cannot be used, and `--deny MCPTool` stays
    effective per xAI's docs. `--always-approve` and `--yolo` exist and are
    not used -- `--permission-mode bypassPermissions` is the measured one.
    `--sandbox` exists and is not claimed; where a named tool may reach is
    the CLI's own business and no promise of this module's.

    WHAT IS NOT MEASURED, said here rather than discovered later. The
    tool-free executor rests on a measured envelope against a real
    subscription answer. This one has no billed tool call yet on any release:
    what is measured is that the vector starts and parses whole in a private
    HOME. That a real answer then uses exactly these tools -- in particular
    the Headless-documented shell ID -- is the half a billed secret-file
    probe has to establish, under the operator's gate, after landing. Until
    it is run, this executor is composed only where an operator armed it by
    name.
    """

    _unsupported_auth_message = (
        "the Grok workspace-tool executor serves subscription profiles only"
    )

    def _invocation_prompt(self, request: AgentExecutionRequestV2) -> str:
        """The job, closed by the shape its node declared for the answer.

        This vector carries no `--json-schema`, so the ask is the job's own last
        words. It is measured against the same 30,000-byte inline bound as the
        job alone, because the bound is about what the carrier transports, not
        about who wrote which part of it.
        """

        return _validated_inline_prompt(
            _job_with_output_schema_ask(
                request.job_bytes, request.declared_output_schema_bytes
            )
        )

    def _invocation_arguments(
        self,
        model: str,
        prompt: str,
        declared_output_schema_bytes: bytes | None,
        maximum_assistant_turns: int | None = None,
    ) -> tuple[str, ...]:
        del declared_output_schema_bytes
        return _workspace_tool_arguments(
            self.settings.executable,
            model,
            prompt,
            maximum_assistant_turns,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """Read one workspace-tool call back from the stream it wrote.

        The stream is what this operation has that its tool-free sibling does
        not: the turns and the doors, in the order the CLI wrote them, and a
        terminal line the CLI names itself rather than one this module picks
        out. Every way through here carries whatever the call did write, the
        refusals included -- what a call wrote before it ended is the only
        account of an ending an exit code explains nothing about.

        The terminal line's `result` is the whole answer and is handed on as
        written. Measured 04.09.2026 on grok 1.0.5 / grok-4.6 without
        `--json-schema` (`#1174`), that text was one bare JSON document in both
        the object-schema and the root-string vector -- no code fence, no prose
        beside it, and no `structured_output` field on the line to read instead.
        So nothing is stripped here: a seam that trimmed shapes nobody measured
        would be deciding what counts as an answer, and that decision belongs to
        the output schema alone.
        """

        del invocation
        session = _streamed_session(completion.standard_output)
        transcript = session.transcript
        if completion.return_code != 0:
            return _unusable_provider_answer(transcript)
        envelope = session.terminal_envelope
        if envelope is None or envelope.get(_ERROR_FLAG_FIELD) is not False:
            return GrokProviderEndedWithoutFinalMessage(transcript)
        if not session.opened_a_door:
            return GrokProviderEndedWithoutToolUse(transcript)
        answer = envelope.get(_RESULT_FIELD)
        if not isinstance(answer, str) or not answer:
            return GrokProviderEndedWithoutFinalMessage(transcript)
        output_bytes = answer.encode("utf-8")
        if len(output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _unusable_provider_answer(transcript)
        return AgentExecutionResult(output_bytes, transcript)


@dataclass(frozen=True)
class GrokWorkspaceToolExecutorFactory:
    """The host-composed factory for one Grok workspace-tool executor."""

    settings: GrokSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return GROK_WORKSPACE_TOOLS_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        """Only headless-with-tools, and the omissions are the guard.

        Plain `HEADLESS` is missing on purpose. A configuration asking for it is
        asking for a call that can touch nothing, and answering that ask with
        this invocation would hand a node tools its binding never requested. The
        tool-free executor serves it, and a binding that names this executor's
        revision while asking for `HEADLESS` is refused by the starter rather
        than quietly widened. Interactive is missing for the same reason it is
        missing from the tool-free executor: there is no terminal here.
        """

        return frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS})

    def open(self) -> GrokWorkspaceToolExecutor:
        return GrokWorkspaceToolExecutor(self.settings)
