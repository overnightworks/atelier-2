"""One headless `codex exec` subscription executor.

Invocation semantics and the version set are measured against codex-cli
0.147.0. `codex exec` has no prompt-file flag, so the job travels on standard
input -- the CLI reads its instructions there when the prompt argument is `-`
-- and never on the argument vector, where any account on the host could read
it. The child environment is only `HOME`, `CODEX_HOME` and `PATH`, and `HOME`
is containment rather than convenience: without it the CLI resolves the
invoking account's own profile.

Every invocation gets one private disposable `HOME`/`CODEX_HOME` instead of
the operator's own (`_open_job_directory`). The seam copies only `auth.json`
into it, with an exclusive no-follow open, then removes the whole home on
every lifecycle path -- success, refusal, an exception, or executor shutdown
alike. codex-0.147.0's own strings name that file as the CLI's credential
fallback store ("Paste or type your API key below. It will be stored locally
in auth.json"), and it is the CLI's own credential store among the several
files the operator's live `CODEX_HOME` keeps at private (0600) permissions;
`config.toml` is deliberately never copied, because this executor's own
containment attestation requires it absent from a served profile.
codex-0.147.0 also issues single-use OAuth refresh tokens and
rewrites its own credential state through an atomic replace ("failed to
atomically replace secrets file at ..."), so two invocations sharing one
`CODEX_HOME` would race a token refresh against each other exactly as #993
found for Claude.

The agent's answer is taken from `--output-last-message`, the file the CLI
documents as carrying the last message. Standard output is framed but not
parsed: this executor was not permitted a billed `codex exec` call, so the
`--json` event stream is unmeasured, and decoding an unmeasured envelope would
be inventing a provider format.

Because that answer arrives beside the process rather than inside it, decoding
takes the invocation it prepared: the runtime opens one executor per registry
key and hands that same object to every attempt, so an executor correlating
through its own state would let overlapping attempts decode each other's
answers into durable results. This executor holds no answer correlation. The
answer is asked for under a bare name, so the CLI writes it into the directory
the attempt leased, and the workspace owner removes it with everything else the
attempt left behind. A declared output schema uses its own private file until
release, because `codex exec --output-schema` requires a path.

Flags alone do not bound what the CLI discovers or what it may do. `codex
doctor --json` reports the config layer, credential home and MCP servers a
composed profile resolves, so the host attests that exact profile with this
executor's own environment. `codex sandbox` runs one command under the CLI's
own Linux sandbox, so a sandboxed binding is refused unless that sandbox can
actually start here: measured on a host without the namespaces bubblewrap
needs, it exits nonzero with `bwrap: loopback: Failed RTM_NEWADDR`, and a
sandbox that cannot start is not a sandbox.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atelier2.adapters.bounded_processes import bounded_process_answer
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
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
    AgentExecutorKey,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    PrintModeExecutor,
)

CODEX_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("openai"), AgentExecutorRevision("codex-subscription/v1")
)
CODEX_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-exec-last-message-output-schema/v2"
)

# `codex exec` writes progress to standard output while the durable answer goes
# to the last-message file, so this executor frames the stream it does not read
# rather than inventing a tighter allowance for an envelope it was not permitted
# to measure. Eight times the durable answer bound is the allowance it has
# always had, now stated as this executor's own number instead of borrowed from
# the port's ceiling: a frame another provider measures must not widen what this
# process may write.
CODEX_SUBSCRIPTION_FRAME_BYTES = 8 * MAXIMUM_AGENT_OUTPUT_BYTES_V2

CONFORMANT_CODEX_VERSIONS = frozenset({(0, 147, 0)})


class CodexSandboxMode(StrEnum):
    """The sandbox policies `codex exec -s` documents on codex-cli 0.147.0."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


_VERSION_FLAG = "--version"
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_VERSION_PROBE_OUTPUT_BYTES = 4_096

_EXEC_COMMAND = "exec"
# Measured against codex-cli 0.147.0 `--help`: each of these names an ambient
# surface a single headless attempt must not reach. They are containment, not
# preference. `--ignore-user-config` keeps the operator's own `config.toml`,
# and the per-project trust it records, out of the child while auth still
# resolves through `CODEX_HOME`.
_IGNORE_USER_CONFIG_FLAG = "--ignore-user-config"
_IGNORE_RULES_FLAG = "--ignore-rules"
_SKIP_GIT_REPOSITORY_CHECK_FLAG = "--skip-git-repo-check"
_EPHEMERAL_FLAG = "--ephemeral"
_COLOR_FLAG = "--color"
_NEVER = "never"
_MODEL_FLAG = "--model"
_SANDBOX_FLAG = "--sandbox"
_LAST_MESSAGE_FLAG = "--output-last-message"
_OUTPUT_SCHEMA_FLAG = "--output-schema"
# `codex exec` reads its instructions from standard input when the prompt
# argument is `-`. A prompt given as an argument would additionally append
# piped input as a `<stdin>` block, so the job is passed one way only.
_PROMPT_FROM_STANDARD_INPUT = "-"

_DOCTOR_COMMAND = "doctor"
_DOCTOR_JSON_FLAG = "--json"
_DOCTOR_OUTPUT_BYTES = 1_048_576
_CHECKS_FIELD = "checks"
_CONFIGURATION_CHECK = "config.load"
_DETAILS_FIELD = "details"
_STATUS_FIELD = "status"
_OK = "ok"
_CREDENTIAL_HOME_DETAIL = "CODEX_HOME"
_USER_CONFIGURATION_DETAIL = "config.toml"
_MISSING = "missing"
_MCP_SERVER_DETAIL = "mcp servers"
_NO_MCP_SERVERS = "0"

_SANDBOX_COMMAND = "sandbox"
_SANDBOX_PROBE_ARGUMENTS = ("--", "/bin/true")

_PROBE_DIRECTORY_PREFIX = "atelier2-codex-probe-"
_ANSWER_FILE_NAME = "last-message"
_OUTPUT_SCHEMA_FILE_PREFIX = "atelier2-codex-output-schema-"

_HOME_VARIABLE = "HOME"
_CREDENTIAL_DIRECTORY_VARIABLE = "CODEX_HOME"
_SEARCH_PATH_VARIABLE = "PATH"

_JOB_DIRECTORY_PREFIX = "atelier2-codex-job-"
_JOB_DIRECTORY_MODE = 0o700
AUTHENTICATION_FILE_NAME = "auth.json"
# The copied credential is handed out read-only: nothing this executor asks
# the CLI to do needs it to rewrite the copy in place, and the CLI's own
# credential rewrites go through a temp-file-plus-rename replace (see the
# module docstring), which needs write access to the directory, not to this
# file. The whole directory is disposed of at the end of the one call it was
# minted for regardless.
_AUTHENTICATION_FILE_MODE = 0o400
_PRIVATE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_MAXIMUM_AUTHENTICATION_FILE_BYTES = 1_048_576

_UNUSABLE_PROVIDER_ANSWER = AgentExecutionFailure(
    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
)


class CodexSubscriptionAuthModeUnsupported(ValueError):
    """A published configuration bound a non-subscription profile to this executor."""


class CodexExecutableUnsupported(ValueError):
    """The named executable is not a Codex CLI this executor was measured against."""


class CodexContainmentUnattested(ValueError):
    """The composed profile discovers, or fails to contain, what it must not."""


def _parsed_version(reported: str) -> tuple[int, int, int] | None:
    """Read `codex-cli 0.147.0` or `0.147.0` as the version."""

    tokens = reported.strip().split()
    if not tokens:
        return None
    leading = tokens[1] if len(tokens) > 1 and not tokens[0][0].isdigit() else tokens[0]
    parts = leading.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def read_codex_version(
    executable: Path,
    search_path: str,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> tuple[int, int, int]:
    """Ask one executable which Codex it is. Runs it with `--version`.

    The probe carries the served child's own executable search path and
    nothing else. Measured on this host, the installed `codex` is a
    `#!/usr/bin/env node` shim: a probe with no `PATH` cannot start it at all,
    so an empty environment would report every real install as unsupported.
    """

    with tempfile.TemporaryDirectory(prefix="atelier2-codex-version-") as probe_root:
        try:
            process = subprocess.Popen(
                (str(executable), _VERSION_FLAG),
                cwd=probe_root,
                env={_SEARCH_PATH_VARIABLE: search_path},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise CodexExecutableUnsupported(
                f"the Codex executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
        try:
            return_code, answer = bounded_process_answer(
                process, timeout_seconds, _VERSION_PROBE_OUTPUT_BYTES
            )
        except OSError as error:
            raise CodexExecutableUnsupported(
                f"the Codex executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
    if return_code != 0:
        raise CodexExecutableUnsupported(
            f"the Codex executable refused {_VERSION_FLAG} with exit code {return_code}"
        )
    version = _parsed_version(answer.decode("utf-8", "replace"))
    if version is None:
        raise CodexExecutableUnsupported(
            f"the Codex executable did not report a version at {_VERSION_FLAG}"
        )
    return version


def verify_codex_capability(
    executable: Path,
    search_path: str,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> tuple[int, int, int]:
    """Refuse an executable outside the reviewed conformance set."""

    version = read_codex_version(executable, search_path, timeout_seconds)
    if version not in CONFORMANT_CODEX_VERSIONS:
        reported = ".".join(str(part) for part in version)
        conformant = ", ".join(
            ".".join(str(part) for part in candidate)
            for candidate in sorted(CONFORMANT_CODEX_VERSIONS)
        )
        raise CodexExecutableUnsupported(
            f"serving Codex subscription agents requires Codex {conformant}, "
            f"not {reported}: this executor's invocation semantics were measured "
            "against that exact release"
        )
    return version


@dataclass(frozen=True)
class CodexSubscriptionSettings:
    executable: Path
    credential_directory: Path
    search_path: str
    sandbox: CodexSandboxMode

    def __post_init__(self) -> None:
        executable = self.executable.resolve()
        credential_directory = self.credential_directory.resolve()
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "credential_directory", credential_directory)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the Codex executable must be an executable file")
        if not credential_directory.is_dir():
            raise ValueError(
                "the Codex credential directory must be an existing directory"
            )
        if not self.search_path.strip():
            raise ValueError("the Codex executable search path must be nonempty")
        if not isinstance(self.sandbox, CodexSandboxMode):
            raise TypeError("the Codex sandbox mode must be a measured policy")


def _child_environment(
    settings: CodexSubscriptionSettings, state_directory: Path
) -> tuple[tuple[str, str], ...]:
    """The complete environment a launched Codex inherits, and nothing else.

    `HOME` and `CODEX_HOME` name `state_directory`, never
    `settings.credential_directory` directly for a launched job -- see
    `_open_job_directory` below, which prepares one private, disposable
    `state_directory` per invocation. The composition-time containment probe
    below is the one caller that deliberately names
    `settings.credential_directory` itself, because it is attesting that
    exact configured directory rather than a job's private copy.
    """

    return (
        (_HOME_VARIABLE, str(state_directory)),
        (_CREDENTIAL_DIRECTORY_VARIABLE, str(state_directory)),
        (_SEARCH_PATH_VARIABLE, settings.search_path),
    )


def _probe(
    settings: CodexSubscriptionSettings,
    arguments: tuple[str, ...],
    timeout_seconds: float,
    output_bytes: int,
    environment_overrides: dict[str, str],
) -> tuple[int, bytes]:
    """Run one non-billed CLI subcommand against the configured credential home.

    A probe attests `settings.credential_directory` itself -- the configured
    profile, not the private per-job copy a real invocation now gets
    (`_open_job_directory`) -- because it is composition-time containment of
    the configuration, not of one call. It runs in a probe root of its own
    rather than in an attempt's lease, which does not exist yet at composition.
    """

    with tempfile.TemporaryDirectory(prefix=_PROBE_DIRECTORY_PREFIX) as probe_root:
        try:
            process = subprocess.Popen(
                (str(settings.executable), *arguments),
                cwd=probe_root,
                env=dict(_child_environment(settings, settings.credential_directory))
                | environment_overrides,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise CodexContainmentUnattested(
                f"the Codex executable did not answer {arguments[0]}: {error}"
            ) from error
        try:
            return bounded_process_answer(process, timeout_seconds, output_bytes)
        except OSError as error:
            raise CodexContainmentUnattested(
                f"the Codex executable did not answer {arguments[0]}: {error}"
            ) from error


def _uncontained_surfaces(
    settings: CodexSubscriptionSettings, reported: dict[str, object]
) -> tuple[str, ...]:
    """Name every way the reported profile is not the one this executor granted."""

    checks = reported.get(_CHECKS_FIELD)
    if not isinstance(checks, dict):
        return (f"{_CHECKS_FIELD} missing",)
    configuration = checks.get(_CONFIGURATION_CHECK)
    if not isinstance(configuration, dict):
        return (f"{_CONFIGURATION_CHECK} missing",)
    details = configuration.get(_DETAILS_FIELD)
    if not isinstance(details, dict):
        return (f"{_CONFIGURATION_CHECK}.{_DETAILS_FIELD} missing",)

    uncontained: list[str] = []
    if configuration.get(_STATUS_FIELD) != _OK:
        uncontained.append(f"{_CONFIGURATION_CHECK}={configuration.get(_STATUS_FIELD)}")
    if details.get(_CREDENTIAL_HOME_DETAIL) != str(settings.credential_directory):
        uncontained.append(
            f"{_CREDENTIAL_HOME_DETAIL}={details.get(_CREDENTIAL_HOME_DETAIL)}"
        )
    # A contained profile reports its user config as present-but-missing; a
    # bare path means the CLI resolved a config.toml it would load.
    user_configuration = details.get(_USER_CONFIGURATION_DETAIL)
    if not (
        isinstance(user_configuration, list)
        and len(user_configuration) == 2
        and user_configuration[1] == _MISSING
    ):
        uncontained.append(_USER_CONFIGURATION_DETAIL)
    mcp_servers = details.get(_MCP_SERVER_DETAIL)
    if mcp_servers not in (None, _NO_MCP_SERVERS):
        uncontained.append(f"{_MCP_SERVER_DETAIL}={mcp_servers}")
    return tuple(uncontained)


def attest_codex_containment(
    settings: CodexSubscriptionSettings,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
    environment_overrides: dict[str, str] | None = None,
) -> None:
    """Refuse to serve unless the composed profile is contained and enforceable.

    Neither probe requests a completion, so attesting costs no subscription.
    """

    overrides = environment_overrides or {}
    # The report is read rather than its exit code: measured on codex-cli
    # 0.147.0, `doctor` exits nonzero whenever any check fails, and the ones
    # that fail on a contained deployment are auth and network reachability --
    # neither of which is containment. Gating on the exit code would refuse to
    # serve a perfectly contained profile on an offline host, so this reads the
    # configuration check it actually depends on and lets a missing credential
    # fail the attempt loudly instead.
    _return_code, answer = _probe(
        settings,
        (_DOCTOR_COMMAND, _DOCTOR_JSON_FLAG),
        timeout_seconds,
        _DOCTOR_OUTPUT_BYTES,
        overrides,
    )
    try:
        reported: object = json.loads(answer)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexContainmentUnattested(
            f"the Codex executable did not report {_DOCTOR_COMMAND} as JSON"
        ) from error
    if not isinstance(reported, dict):
        raise CodexContainmentUnattested(
            f"the Codex executable did not report {_DOCTOR_COMMAND} as an object"
        )
    uncontained = _uncontained_surfaces(settings, reported)
    if uncontained:
        raise CodexContainmentUnattested(
            "serving Codex subscription agents requires a profile that resolves "
            "this executor's own credential home, loads no user config.toml and "
            f"configures no MCP server; this deployment reports "
            f"{', '.join(uncontained)}"
        )

    if settings.sandbox is CodexSandboxMode.DANGER_FULL_ACCESS:
        return
    sandbox_return_code, _sandbox_answer = _probe(
        settings,
        (_SANDBOX_COMMAND, *_SANDBOX_PROBE_ARGUMENTS),
        timeout_seconds,
        _DOCTOR_OUTPUT_BYTES,
        overrides,
    )
    if sandbox_return_code != 0:
        raise CodexContainmentUnattested(
            f"serving Codex agents under the {settings.sandbox.value} sandbox "
            f"requires a host where {_SANDBOX_COMMAND} can start; it exited with "
            f"code {sandbox_return_code} here, and a sandbox that cannot start "
            "does not contain anything"
        )


def _write_private_file(path: Path, payload: bytes, mode: int) -> None:
    """Create one file only this call may have named, and nothing else can follow.

    `O_EXCL` refuses a name that already exists and `O_NOFOLLOW` refuses a
    symlink -- both matter here because the directory this writes into is
    freshly minted per invocation and every entry in it is this seam's own.
    """

    descriptor = os.open(path, _PRIVATE_FILE_FLAGS, mode)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("private Codex config file write made no progress")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def _authentication_bytes(settings: CodexSubscriptionSettings) -> bytes:
    """Read the operator's own `auth.json`, and nothing else it holds.

    Established from the pinned codex-0.147.0 executable's own strings by
    static inspection (`strings`, never a billed invocation): its CLI auth
    storage falls back to exactly this file when no OS keyring is used
    ("Paste or type your API key below. It will be stored locally in
    auth.json"). Several other files under the operator's own live
    `CODEX_HOME` also carry private (0600) permissions, but `auth.json` is the
    CLI's own credential store among them; `config.toml` in particular is
    never copied, because this executor's own containment attestation
    (`attest_codex_containment` above) requires it absent from a served
    profile.
    """

    path = settings.credential_directory / AUTHENTICATION_FILE_NAME
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o077:
            raise ValueError(
                f"the Codex {AUTHENTICATION_FILE_NAME} must be a private regular file"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > _MAXIMUM_AUTHENTICATION_FILE_BYTES:
                raise ValueError(
                    f"the Codex {AUTHENTICATION_FILE_NAME} exceeds its private "
                    "copy bound"
                )
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _open_job_directory(settings: CodexSubscriptionSettings) -> Path:
    """Prepare one private, disposable Codex home.

    Every invocation this module prepares gets one of these instead of the
    operator's own `CODEX_HOME`: a private copy of `auth.json` on a path the
    operator's own live directory never sees a launched process open. Cleanup
    on every path -- success, refusal, timeout, cancellation, exception and a
    killed process alike -- is the caller's contract, carried out through the
    executor's own `_job_directories` bookkeeping (mirroring
    `_ClaudeJobDirectories` in `claude_subscription`); this function's own
    contract is narrower: leave nothing behind when *it* fails to finish
    preparing the directory.
    """

    directory = Path(tempfile.mkdtemp(prefix=_JOB_DIRECTORY_PREFIX))
    os.chmod(directory, _JOB_DIRECTORY_MODE)
    prepared = False
    try:
        _write_private_file(
            directory / AUTHENTICATION_FILE_NAME,
            _authentication_bytes(settings),
            _AUTHENTICATION_FILE_MODE,
        )
        prepared = True
        return directory
    finally:
        if not prepared:
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass


def _answer_file_of(invocation: AgentProcessInvocation) -> Path:
    """Name the answer file inside the directory this attempt leased.

    The command asks for the answer under a bare name, so the CLI writes it
    into the directory it is started in -- the attempt's own lease. There is no
    second private directory to create, hand over or remove: the lease is that
    directory, and the workspace owner removes it with everything in it.
    """

    return invocation.lease.working_directory / _ANSWER_FILE_NAME


def _write_output_schema(document: bytes) -> Path:
    """Store this invocation's exact schema in one private disposable file."""

    descriptor, name = tempfile.mkstemp(
        prefix=_OUTPUT_SCHEMA_FILE_PREFIX, suffix=".json"
    )
    path = Path(name)
    try:
        remaining = memoryview(document)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("Codex output schema write made no progress")
            remaining = remaining[written:]
        return path
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class CodexSubscriptionProcessCommand(AgentProcessCommand):
    """One Codex command and, where declared, its private schema file."""

    output_schema_path: Path | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class CodexSubscriptionExecutor(PrintModeExecutor):
    settings: CodexSubscriptionSettings
    _job_directories: set[Path] = field(
        default_factory=set, init=False, compare=False, repr=False
    )
    _output_schema_paths: set[Path] = field(
        default_factory=set, init=False, compare=False, repr=False
    )
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, compare=False, repr=False
    )
    _closed: threading.Event = field(
        default_factory=threading.Event, init=False, compare=False, repr=False
    )

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise CodexSubscriptionAuthModeUnsupported(
                "the Codex subscription executor serves subscription profiles only"
            )
        settings = self.settings
        state_directory = _open_job_directory(settings)
        registered = False
        output_schema_path: Path | None = None
        try:
            output_schema_path = (
                None
                if request.declared_output_schema_bytes is None
                else _write_output_schema(request.declared_output_schema_bytes)
            )
            command = CodexSubscriptionProcessCommand(
                (
                    str(settings.executable),
                    _EXEC_COMMAND,
                    _IGNORE_USER_CONFIG_FLAG,
                    _IGNORE_RULES_FLAG,
                    _SKIP_GIT_REPOSITORY_CHECK_FLAG,
                    _EPHEMERAL_FLAG,
                    _COLOR_FLAG,
                    _NEVER,
                    _MODEL_FLAG,
                    binding.configuration.model,
                    _SANDBOX_FLAG,
                    settings.sandbox.value,
                    _LAST_MESSAGE_FLAG,
                    _ANSWER_FILE_NAME,
                    *(
                        ()
                        if output_schema_path is None
                        else (_OUTPUT_SCHEMA_FLAG, str(output_schema_path))
                    ),
                    _PROMPT_FROM_STANDARD_INPUT,
                ),
                _child_environment(settings, state_directory),
                request.job_bytes,
                standard_output_frame_bytes=CODEX_SUBSCRIPTION_FRAME_BYTES,
                output_schema_path=output_schema_path,
            )
            with self._lifecycle_lock:
                if self._closed.is_set():
                    raise RuntimeError("the Codex executor is closed")
                self._job_directories.add(state_directory)
                if output_schema_path is not None:
                    self._output_schema_paths.add(output_schema_path)
                registered = True
            return command
        finally:
            if not registered:
                try:
                    shutil.rmtree(state_directory)
                except FileNotFoundError:
                    pass
                if output_schema_path is not None:
                    try:
                        output_schema_path.unlink()
                    except FileNotFoundError:
                        pass

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """Read the answer this exact invocation's lease holds."""

        try:
            answer = _answer_file_of(invocation).read_bytes()
        except OSError:
            answer = None
        if completion.return_code != 0:
            return _UNUSABLE_PROVIDER_ANSWER
        if len(completion.standard_output) > CODEX_SUBSCRIPTION_FRAME_BYTES:
            return _UNUSABLE_PROVIDER_ANSWER
        if answer is None or len(answer) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _UNUSABLE_PROVIDER_ANSWER
        return AgentExecutionResult(answer)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Take back the private home and schema file this invocation was handed.

        `CODEX_HOME` now names a private directory holding a copy of the
        operator's own `auth.json`, prepared for this call alone
        (`_open_job_directory`), so it is taken back on every path -- success,
        refusal, an exception, or a killed process -- exactly as an output
        schema file already was. The answer remains in the attempt's leased
        workspace directory and is not this method's concern.
        """

        environment = dict(command.environment)
        home = environment.get(_HOME_VARIABLE)
        codex_home = environment.get(_CREDENTIAL_DIRECTORY_VARIABLE)
        if home is None or home != codex_home:
            raise ValueError("Codex invocation state binding is missing")
        state_directory = Path(home)
        output_schema_path = (
            command.output_schema_path
            if isinstance(command, CodexSubscriptionProcessCommand)
            else None
        )
        with self._lifecycle_lock:
            if state_directory in self._job_directories:
                try:
                    shutil.rmtree(state_directory)
                except FileNotFoundError:
                    pass
                self._job_directories.remove(state_directory)
            if (
                output_schema_path is not None
                and output_schema_path in self._output_schema_paths
            ):
                try:
                    output_schema_path.unlink()
                except FileNotFoundError:
                    pass
                self._output_schema_paths.remove(output_schema_path)

    def close(self) -> None:
        """Take back every job home and schema file that never reached release."""

        with self._lifecycle_lock:
            self._closed.set()
            directories = tuple(self._job_directories)
            paths = tuple(self._output_schema_paths)
            errors: list[Exception] = []
            for directory in directories:
                try:
                    shutil.rmtree(directory)
                except FileNotFoundError:
                    self._job_directories.remove(directory)
                except OSError as error:
                    errors.append(error)
                else:
                    self._job_directories.remove(directory)
            for path in paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    self._output_schema_paths.remove(path)
                except OSError as error:
                    errors.append(error)
                else:
                    self._output_schema_paths.remove(path)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Codex invocation cleanup failed", errors)


@dataclass(frozen=True)
class CodexSubscriptionExecutorFactory:
    settings: CodexSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return CODEX_SUBSCRIPTION_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return CODEX_SUBSCRIPTION_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return frozenset({AgentExecutionCapability.HEADLESS})

    def open(self) -> CodexSubscriptionExecutor:
        return CodexSubscriptionExecutor(self.settings)
