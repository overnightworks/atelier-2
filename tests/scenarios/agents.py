from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from sqlalchemy.engine import Engine

from atelier2.adapters.agent_workspaces import (
    SCRATCH_ROOT_MODE,
    LocalAgentAttemptWorkspaceOwner,
)
from atelier2.adapters.claude_subscription import (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    CONFORMANT_CLAUDE_VERSIONS,
    CREDENTIAL_RECORD_ENTRY,
    ClaudeSubscriptionExecutorFactory,
    ClaudeSubscriptionSettings,
    ClaudeWorkspaceToolExecutorFactory,
)
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agent_permissions import (
    GRANTS_NOTHING,
    MINIMUM_PERMISSION_CALL_ORDINAL,
    PermissionCorrelationId,
    PermissionDecision,
    PermissionEffect,
    PermissionRequest,
    PermissionScope,
    PolicyPermissionDecider,
)
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    ToolCalled,
    ToolReturned,
    UnrecognisedProviderOutput,
    Usage,
)
from atelier2.contracts.agents import (
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
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
)
from atelier2.contracts.host_configuration import (
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ProviderModelCheck,
)
from atelier2.contracts.process_endings import ProcessExitSignature
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentExecutionFailure,
    AgentExecutorKey,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    AgentSession,
    PermissionDecider,
    ProviderConversationBinding,
)
from atelier2.ports.durable_runs import (
    DurablePublishedRunResult,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.host_configuration import (
    ModelRegistryRevisionCreated,
    ModelRegistryRevisionExisting,
)
from atelier2.ports.provider_conversations import (
    ProviderCancellationCause,
    ProviderConversationBounds,
    ProviderConversationClosing,
    ProviderConversationEnding,
    ProviderFilesystemAccess,
    ProviderFilesystemAuthority,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderStandardInput,
    ProviderTerminalOutcome,
    ProviderTerminalReason,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA

SCENARIO_PROVIDER_FRAME_BYTES = 49_152
"""The raw stdout frame a scenario provider declares when it is not the subject."""

# The #666 Log-tab e2e plants this sentence in the reviewer instruction so the
# fixture server's existing e2e-v3 recording executor writes the raw canary to
# stdout and keeps a transcript. serve_cockpit.py cannot grow a second factory
# while another lane holds it.
E2E_LOG_TAB_INSTRUCTION_MARK = "atelier2-e2e-log-tab-canary-666"
E2E_LOG_TAB_CANARY = "sk-ant" + "-plantedcanarysecret0123456789"


def e2e_log_tab_stdout() -> bytes:
    return (
        "checking changed files\n"
        f"canary credential: {E2E_LOG_TAB_CANARY}\n"
        "command stopped before an answer"
    ).encode()


def e2e_log_tab_request(request: object) -> bool:
    # Drain proofs plant a run_id-only request on this same executor.
    job_bytes = getattr(request, "job_bytes", None)
    return isinstance(job_bytes, bytes) and (
        E2E_LOG_TAB_INSTRUCTION_MARK.encode() in job_bytes
    )


def e2e_log_tab_command(request: AgentExecutionRequestV2) -> AgentProcessCommand:
    return launching(
        sys.executable,
        "-c",
        "import os, sys; os.write(1, bytes.fromhex(sys.argv[1])); sys.exit(int(sys.argv[2]))",
        e2e_log_tab_stdout().hex(),
        "1",
    )(request)


def e2e_log_tab_decode(
    completion: AgentProcessCompletion,
) -> AgentExecutionFailure:
    stdout = completion.standard_output.decode("utf-8")
    return AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        AttemptTranscript.of(
            (
                AssistantTurn(
                    "I will check the changed files against the review brief."
                ),
                ToolCalled(
                    "Read",
                    '{"path":"docs/requirements/0003-ziel-ui.md"}',
                ),
                ToolReturned("Read", "Read 128 lines."),
                AssistantTurn(
                    "The acceptance sentence and the picture still disagree."
                ),
                ToolCalled("Bash", '{"command":"check changed files"}'),
                ToolReturned("Bash", "Command ended before it returned an answer."),
                Usage(12_400, 680),
                UnrecognisedProviderOutput(stdout),
            )
        ),
    )


def publish_checked_model_registry(
    engine: Engine,
    provider: ProviderId,
    configurations: tuple[AgentConfigurationRevision, ...],
) -> None:
    """Make the exact configurations a direct V3-start scenario names eligible."""
    channel = DbosHostConfigurationChannel(engine)
    current = channel.latest_model_registry_revision(provider)
    assert current is None or isinstance(current, ModelRegistryRevision), current
    current_entries = () if current is None else current.entries
    entries_by_model = {entry.model_id: entry for entry in current_entries}
    entries_by_model.update(
        {
            configuration.model: ModelRegistryEntry(
                configuration.model,
                configuration.revision_hash,
                ModelRegistryEntrySource.OPERATOR,
                ProviderModelCheck.CHECKED,
            )
            for configuration in configurations
        }
    )
    revision = ModelRegistryRevision(
        provider,
        1 if current is None else current.revision_number + 1,
        tuple(entries_by_model.values()),
    )
    published = channel.publish_model_registry_revision(revision)
    assert isinstance(
        published, (ModelRegistryRevisionCreated, ModelRegistryRevisionExisting)
    )


def agent_scratch_root(directory: Path) -> Path:
    """One blank scratch root of the exact shape the workspace owner accepts."""

    root = directory / "agent-scratch"
    # A crash scenario restarts against the same root, exactly as a server does.
    root.mkdir(mode=SCRATCH_ROOT_MODE, parents=True, exist_ok=True)
    return root


def agent_workspace_owner(directory: Path) -> LocalAgentAttemptWorkspaceOwner:
    return LocalAgentAttemptWorkspaceOwner(agent_scratch_root(directory))


def runtime_workspace_owner(runtime: DbosRuntime) -> LocalAgentAttemptWorkspaceOwner:
    """The workspace owner a runtime serving provider executors always has."""

    owner = runtime.agent_workspace_owner
    assert owner is not None
    return owner


def leased_directory_identity(
    attempt_id: AgentAttemptId, working_directory: Path
) -> AgentAttemptWorkspaceLease:
    """One lease over a directory a test houses itself, with its real identity.

    The identity is read from the directory rather than invented, because that
    is what the launcher compares against: a made-up device and inode would make
    every test refuse, and a zero pair would make the comparison meaningless.
    """

    working_directory.mkdir(parents=True, exist_ok=True)
    standing = os.stat(working_directory, follow_symlinks=False)
    return AgentAttemptWorkspaceLease(
        attempt_id, working_directory, standing.st_dev, standing.st_ino
    )


def process_invocation(
    attempt_id: AgentAttemptId,
    arguments: tuple[str, ...],
    working_directory: Path,
    environment: tuple[tuple[str, str], ...] = (),
    standard_input: bytes = b"",
    *,
    standard_output_frame_bytes: int = SCENARIO_PROVIDER_FRAME_BYTES,
    conversation: ProviderConversationBinding | None = None,
) -> AgentProcessInvocation:
    """One invocation for a test that supervises a process it houses itself.

    Tests of the process seam own the directory they run a child in, so they
    lease it by hand rather than standing up the scratch root the application
    layer leases from.
    """

    return AgentProcessInvocation(
        AgentProcessCommand(
            arguments,
            environment,
            standard_input,
            standard_output_frame_bytes=standard_output_frame_bytes,
        ),
        leased_directory_identity(attempt_id, working_directory),
        conversation,
    )


MEASURED_CLAUDE_VERSION = ".".join(
    str(part) for part in max(CONFORMANT_CLAUDE_VERSIONS)
)


def _version_answering(program: str, version: str | None) -> str:
    """Wrap a fake CLI so it answers `--version` the way the real one does.

    The deployment reads the executable's version before it composes anything,
    so a fake that cannot answer is not a fake of this CLI at all.
    """

    if version is None:
        return program
    return (
        "import sys\n"
        'if "--version" in sys.argv:\n'
        f'    print("{version} (Claude Code)")\n'
        "    raise SystemExit(0)\n"
    ) + program


def claude_search_path(directory: Path) -> str:
    """A search path carrying the bubblewrap the scrubbing CLI insists on.

    The CLI resolves it through the search path this deployment hands the
    launched process, so a deployment fake needs one there or it is not a
    deployment this executor accepts.
    """

    tools = directory / "tools"
    tools.mkdir(exist_ok=True)
    bubblewrap = tools / "bwrap"
    bubblewrap.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    bubblewrap.chmod(0o755)
    return str(tools)


PERSONAL_SUBSCRIPTION_TYPE = "max"
"""What a deployment fake's credential record says the account is.

This executor is contracted for a personal subscription, so a fake credential
directory carries the record a personal account leaves behind. Without one no
fake deployment could be attested at all.
"""


def claude_subscription_deployment(
    directory: Path,
    program: str,
    version: str | None = MEASURED_CLAUDE_VERSION,
    subscription_type: str | None = PERSONAL_SUBSCRIPTION_TYPE,
) -> ClaudeSubscriptionSettings:
    """Deploy one executable Python program in place of the Claude CLI."""

    executable = directory / "claude"
    executable.write_text(
        f"#!{sys.executable}\n{_version_answering(program, version)}",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    credentials = directory / "credentials"
    credentials.mkdir()
    # The credential record an authenticated CLI would have left behind.
    (credentials / CREDENTIAL_RECORD_ENTRY).write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": subscription_type}}),
        encoding="utf-8",
    )
    return ClaudeSubscriptionSettings(
        executable, credentials, claude_search_path(directory)
    )


CLAUDE_SUBSCRIPTION_WORKFLOW = f"""format_version: 3
name: Claude subscription host document
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


def claude_subscription_runtime(
    root: Path,
    settings: ClaudeSubscriptionSettings,
    *,
    workspace_tools: bool = False,
) -> DbosRuntime:
    """The production runtime, serving the Claude executors this scenario arms.

    The workspace-tool executor is a second, separately armed grant of the same
    deployment, so a scenario asks for it the way an operator does instead of
    getting it because a Claude executable was named.
    """

    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "claude-subscription-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("claude-subscription-test"),
        ),
        (
            ClaudeSubscriptionExecutorFactory(settings),
            *(
                (ClaudeWorkspaceToolExecutorFactory(settings),)
                if workspace_tools
                else ()
            ),
        ),
    )


def claude_subscription_publication(
    runtime: DbosRuntime,
    model: str = "claude-haiku-4-5",
    requested_capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
    executor_revision: AgentExecutorRevision = (
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision
    ),
) -> tuple[AgentConfigurationRevision, WorkflowRevision]:
    """Publish one configuration and workflow through the production catalog.

    The catalog binds a configuration to an executor key, not to a capability,
    so a configuration demanding what this executor cannot serve is published
    here exactly as a real one would be -- and refused where the demand is
    actually read.
    """

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        executor_revision,
        requested_capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog.publish_agent_configuration_revision(configuration)
    publish_checked_model_registry(
        runtime.engine, ProviderId("anthropic"), (configuration,)
    )
    DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    workflow = WorkflowRevision(CLAUDE_SUBSCRIPTION_WORKFLOW)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return configuration, workflow


def claude_subscription_start(
    runtime: DbosRuntime,
    run_name: str,
    model: str = "claude-haiku-4-5",
    requested_capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
    executor_revision: AgentExecutorRevision = (
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision
    ),
) -> tuple[DurablePublishedRunResult, WorkflowRevision]:
    """Ask the production starter for one run bound to this executor."""

    configuration, workflow = claude_subscription_publication(
        runtime, model, requested_capability, executor_revision
    )
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


def claude_subscription_attempt(
    runtime: DbosRuntime,
    run_name: str,
    model: str = "claude-haiku-4-5",
    requested_capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
    executor_revision: AgentExecutorRevision = (
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision
    ),
    operational_identity: AgentExecutorOperationalIdentity = (
        CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY
    ),
) -> AgentAttemptExecution:
    """One durable run whose only agent node is bound to this executor."""

    run_id = RunId(run_name)
    started, workflow = claude_subscription_start(
        runtime, run_name, model, requested_capability, executor_revision
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
            b"build",
        )
    )


def resolved_agent_binding(
    role: str = "builder",
    *,
    model: str = "opus",
    provider_id: str = "anthropic",
    executor_revision: str = "claude-cli/v1",
    requested_capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
    revision_format_version: AgentConfigurationRevisionFormatVersion = (
        AgentConfigurationRevisionFormatVersion.V2
    ),
) -> ResolvedAgentBinding:
    """What one role of a started run resolved to, hashed as the start hashes it."""

    auth = AuthProfileRevision("max", 1, ProviderId(provider_id), AuthMode.SUBSCRIPTION)
    return ResolvedAgentBinding(
        AgentRole(role),
        AgentConfigurationRevision(
            model,
            auth.revision_hash,
            AgentExecutorRevision(executor_revision),
            requested_capability,
            revision_format_version,
        ),
        auth,
    )


def agent_execution_request_v2(
    run_name: str = "scenario/one-agent",
    node_id: str = "builder",
    executor_revision: str = "scenario-cli/v1",
    operational_identity: str = "controlled-process",
) -> AgentExecutionRequestV2:
    """One V2 request bound to a plausible role, for scenarios with no store.

    The binding is real rather than a stub because the request hashes it, and an
    attempt identity derived from a half-built binding would not be the identity
    the durable path mints.
    """

    run_id = RunId(run_name)
    revision = WorkflowRevisionHash("2" * 64)
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision(executor_revision),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision, node_id),
        run_id,
        revision,
        node_id,
        ResolvedAgentBinding(AgentRole(node_id), configuration, auth),
        AgentExecutorOperationalIdentity(operational_identity),
        b"build",
    )


def prepared_agent_attempt(execution: AgentAttemptExecution) -> AgentAttempt:
    """The attempt a store hands back before anything has been claimed."""

    request = execution.request
    return AgentAttempt(
        execution.attempt_id,
        request.node_execution_id,
        request.request_hash,
        request.executor_operational_identity,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.ordinal,
        AgentAttemptState.PREPARED,
        0,
    )


def agent_attempt_execution(
    request: AgentExecutionRequestV2, ordinal: int = 1
) -> AgentAttemptExecution:
    return AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(
            request.node_execution_id, request.request_hash, ordinal
        ),
        ordinal,
    )


AgentCommandFactory = Callable[[AgentExecutionRequestV2], AgentProcessCommand]
"""How a scenario executor turns a request into the process it launches."""

AgentCompletionDecoder = Callable[
    [AgentProcessCompletion], AgentExecutionResult | AgentExecutionFailure
]
"""How a scenario executor reads a finished process back."""

_PROVIDER_WRITES_HEX_AFTER_DELAY = (
    "import os, sys, time; time.sleep(float(sys.argv[1])); "
    "os.write(1, bytes.fromhex(sys.argv[2]))"
)


def launching(
    *arguments: str, frame_bytes: int = SCENARIO_PROVIDER_FRAME_BYTES
) -> AgentCommandFactory:
    """A command launching exactly these arguments, whatever the request says."""

    def command(request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(arguments, standard_output_frame_bytes=frame_bytes)

    return command


def emitting(
    output: bytes,
    *,
    delay_seconds: float = 0,
    frame_bytes: int = SCENARIO_PROVIDER_FRAME_BYTES,
) -> AgentCommandFactory:
    """A command whose process writes exactly these bytes to standard output."""

    return launching(
        sys.executable,
        "-c",
        _PROVIDER_WRITES_HEX_AFTER_DELAY,
        str(delay_seconds),
        output.hex(),
        frame_bytes=frame_bytes,
    )


_PROVIDER_WRITES_A_FILE_THEN_ANSWERS = (
    "import os, pathlib, sys; "
    "pathlib.Path(sys.argv[1]).write_text(sys.argv[2]); "
    "os.write(1, bytes.fromhex(sys.argv[3]))"
)


def working_and_emitting(
    output: bytes,
    name: str,
    text: str,
    *,
    frame_bytes: int = SCENARIO_PROVIDER_FRAME_BYTES,
) -> AgentCommandFactory:
    """A command that leaves one file in the leased directory, then answers.

    An attempt that leaves the pinned tree untouched now ends under its own
    name before any check runs, so every scenario whose subject is what happens
    *after* the work -- the grant, the verification, the keeping -- needs a
    provider that actually did some.
    """

    return launching(
        sys.executable,
        "-c",
        _PROVIDER_WRITES_A_FILE_THEN_ANSWERS,
        name,
        text,
        output.hex(),
        frame_bytes=frame_bytes,
    )


def answering_each_execution(
    answers: Mapping[tuple[str, int], bytes],
) -> AgentCommandFactory:
    """A command whose process writes the answer this exact node and round owes.

    A scenario where a round's answer decides what happens next needs a provider
    that can say something different in the second round than in the first, and
    the request already carries which execution it is. A missing entry raises
    rather than falling back on a default: the scenario would otherwise be
    proving something about an answer nobody wrote down.
    """

    def command(request: AgentExecutionRequestV2) -> AgentProcessCommand:
        return emitting(answers[(request.node_id, request.round_ordinal)])(request)

    return command


_PROVIDER_WRITES_STANDARD_ERROR_AND_EXITS = "import os, sys; os.write(2, bytes.fromhex(sys.argv[1])); sys.exit(int(sys.argv[2]))"


def dying(
    return_code: int,
    standard_error: bytes = b"",
    *,
    frame_bytes: int = SCENARIO_PROVIDER_FRAME_BYTES,
) -> AgentCommandFactory:
    """A command whose process says exactly this on standard error and ends thus.

    A real child rather than a decoder answering a failure, because what a dead
    provider left behind only exists if a process really wrote it: supervision
    has to have collected the exit status and the standard error for anything
    downstream to be able to name them.
    """

    return launching(
        sys.executable,
        "-c",
        _PROVIDER_WRITES_STANDARD_ERROR_AND_EXITS,
        standard_error.hex(),
        str(return_code),
        frame_bytes=frame_bytes,
    )


def refusing(reason: str) -> Callable[[Any], Never]:
    """A command or decoder for an executor this scenario must never reach."""

    def refuse(_unreached: Any) -> Never:
        raise AssertionError(reason)

    return refuse


def decode_process_exit(
    completion: AgentProcessCompletion,
) -> AgentExecutionResult | AgentExecutionFailure:
    """Read standard output back, or name the exit that was not successful."""

    if completion.return_code != 0:
        return AgentExecutionFailure(
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
        )
    return AgentExecutionResult(completion.standard_output)


def process_exit(
    return_code: int = 1, standard_error: bytes = b""
) -> ProcessExitSignature:
    """What supervision saw of a child no executor could read an answer from.

    The defaults are the plainest such ending -- a nonzero exit that said
    nothing -- for the scenarios whose subject is the write, not the exit.
    """

    return ProcessExitSignature(return_code, standard_error)


def decoding_to(output: bytes) -> AgentCompletionDecoder:
    """A decoder reading every successful process back as exactly these bytes."""

    def decode(
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult | AgentExecutionFailure:
        decoded = decode_process_exit(completion)
        if isinstance(decoded, AgentExecutionFailure):
            return decoded
        return AgentExecutionResult(output)

    return decode


def answering(
    outcome: AgentExecutionResult | AgentExecutionFailure,
) -> AgentCompletionDecoder:
    """A decoder answering this outcome for any process, however it exited."""

    def decode(
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult | AgentExecutionFailure:
        del completion
        return outcome

    return decode


@dataclass
class FilesTheExecutorMayNotReach:
    """The access an executor puts into the conversation it opens.

    Never answered: the dispatch replaces it with the deployment's own, so a
    scenario whose request lands here has proven the executor granted itself
    the workspace.
    """

    def answer(self, request: ProviderFilesystemRequest) -> ProviderFilesystemReply:
        raise AssertionError(
            f"the executor's own file access was reached for {request.path}"
        )


@dataclass
class ConversationNobodyDrives:
    """A `ProviderConversation` for a scenario whose session is faked.

    It declares bounds -- the dispatch reads its reply bound to size the file
    access -- and says nothing, because `FakeAgentSession` never relays it.
    """

    bounds: ProviderConversationBounds = field(
        default_factory=lambda: ProviderConversationBounds(
            SCENARIO_PROVIDER_FRAME_BYTES, 4_096, 4_096, 4_096, 8_192
        )
    )
    incomplete_frame_bytes: int = 0

    def open(self) -> tuple[()]:
        return ()

    def receive_output(self, chunk: bytes) -> tuple[()]:
        del chunk
        return ()

    def input_written(self, written_bytes: int) -> tuple[()]:
        del written_bytes
        return ()

    def answer_permission(self, decision: PermissionDecision) -> ProviderStandardInput:
        del decision
        return ProviderStandardInput(b"")

    def answer_filesystem(
        self, reply: ProviderFilesystemReply
    ) -> ProviderStandardInput:
        del reply
        return ProviderStandardInput(b"")

    def finish(self, ending: ProviderConversationEnding) -> ProviderConversationClosing:
        del ending
        return ProviderConversationClosing(
            ProviderTerminalOutcome(ProviderTerminalReason.ENDED)
        )


SCENARIO_CONVERSING_REVISION = AgentExecutorRevision("scenario-conversation/v1")


def conversation_nobody_drives() -> ProviderConversationBinding:
    """The binding a conversing scenario executor opens, files left to the dispatch."""

    return ProviderConversationBinding(
        SCENARIO_CONVERSING_REVISION,
        ConversationNobodyDrives(),
        FilesTheExecutorMayNotReach(),
    )


def workspace_files_nobody_opens(
    lease: AgentAttemptWorkspaceLease,
    maximum_read_bytes: int,
    authority: ProviderFilesystemAuthority,
) -> ProviderFilesystemAccess:
    """The opener a print-mode scenario hands the dispatch: never called.

    A print-mode executor opens no conversation, so the dispatch has no files
    to bind; reaching this is the dispatch binding files nobody can ask for.
    """

    del maximum_read_bytes, authority
    raise AssertionError(f"no conversation was opened, yet files were bound to {lease}")


@dataclass
class RecordingAgentExecutorV2:
    output: bytes = b""
    requests: list[AgentExecutionRequestV2] = field(default_factory=list)
    lifecycle: list[str] = field(default_factory=list)
    name: str = "recording"
    closes: int = 0
    command: AgentCommandFactory | None = None
    """How the process is launched; by default it writes `output` and exits."""
    decoder: AgentCompletionDecoder = decode_process_exit
    close_failure: BaseException | None = None
    """Raised once the close has been recorded, for cleanup-failure scenarios."""
    completions: list[AgentProcessCompletion] = field(default_factory=list)
    results: list[AgentExecutionResult | AgentExecutionFailure] = field(
        default_factory=list
    )
    released_commands: list[AgentProcessCommand] = field(default_factory=list)
    conversation: ProviderConversationBinding | None = None
    """What `open_conversation` answers; print mode, by default."""

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        self.requests.append(request)
        self.lifecycle.append(f"execute:{self.name}")
        if e2e_log_tab_request(request):
            return e2e_log_tab_command(request)
        return (self.command or emitting(self.output))(request)

    def open_conversation(
        self,
        request: AgentExecutionRequestV2,
        command: AgentProcessCommand,
        lease: AgentAttemptWorkspaceLease,
    ) -> ProviderConversationBinding | None:
        del request, command, lease
        return self.conversation

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        self.completions.append(completion)
        request = self.requests[-1]
        result = (
            e2e_log_tab_decode(completion)
            if e2e_log_tab_request(request)
            else self.decoder(completion)
        )
        self.results.append(result)
        return result

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        self.released_commands.append(command)

    def close(self) -> None:
        self.closes += 1
        self.lifecycle.append(f"close:{self.name}")
        if self.close_failure is not None:
            raise self.close_failure


@dataclass
class RecordingAgentExecutorFactoryV2:
    provider: str
    revision: str
    operational_identity_value: str
    output: bytes
    lifecycle: list[str] = field(default_factory=list)
    capability_set: frozenset[AgentExecutionCapability] = field(
        default_factory=lambda: frozenset({AgentExecutionCapability.HEADLESS})
    )
    key_reads: int = 0
    identity_reads: int = 0
    opens: int = 0
    opened: RecordingAgentExecutorV2 | None = None
    command: AgentCommandFactory | None = None
    decoder: AgentCompletionDecoder = decode_process_exit
    open_failure: BaseException | None = None
    """Raised once the open has been recorded, for open-failure scenarios."""
    close_failure: BaseException | None = None

    @property
    def key(self) -> AgentExecutorKey:
        self.key_reads += 1
        return AgentExecutorKey(
            ProviderId(self.provider), AgentExecutorRevision(self.revision)
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        self.identity_reads += 1
        return AgentExecutorOperationalIdentity(self.operational_identity_value)

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return self.capability_set

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        if self.open_failure is not None:
            raise self.open_failure
        self.opened = RecordingAgentExecutorV2(
            self.output,
            [],
            self.lifecycle,
            self.provider,
            command=self.command,
            decoder=self.decoder,
            close_failure=self.close_failure,
        )
        return self.opened


def failing_agent_executor_factory(
    provider: str,
    lifecycle: list[str],
    *,
    open_failure: BaseException | None = None,
    close_failure: BaseException | None = None,
) -> RecordingAgentExecutorFactoryV2:
    """A factory named after its provider whose open or close fails as told."""

    return RecordingAgentExecutorFactoryV2(
        provider,
        f"{provider}/v1",
        f"{provider}-operation",
        b"",
        lifecycle,
        open_failure=open_failure,
        close_failure=close_failure,
    )


@dataclass
class FakeAgentSession(AgentSession):
    """The `AgentSession` an application test drives when no child should run.

    Every launch answers with the completion the scenario scripted, so a test
    of what an attempt *does* with a provider's ending never pays for
    supervision -- which the process-seam integration tests own and prove on
    their own.

    Stopping is deliberately not faked: a disposition invented here would be
    the one thing about a cancelled attempt that nobody supervised, so a
    scenario that stops an attempt drives the real session instead.
    """

    completion: AgentProcessCompletion = field(
        default_factory=lambda: AgentProcessCompletion(0, b"", b"")
    )
    """The one ending every launch answers with; by default a clean, silent exit."""

    asks: tuple[tuple[PermissionEffect, PermissionScope], ...] = ()
    """What this provider asks for, in order, before its process ends."""

    answers: list[PermissionDecision] = field(default_factory=list)
    """What it was told, so a scenario can read the decision the run produced."""

    file_requests: tuple[ProviderFilesystemRequest, ...] = ()
    """The files this provider asks for, in order, after its permission questions."""

    file_replies: list[ProviderFilesystemReply] = field(default_factory=list)
    """What the bound access answered each file request with."""

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        return prepared_agent_attempt(execution)

    def launch_and_wait(
        self,
        execution: AgentAttemptExecution,
        invocation: AgentProcessInvocation,
        permissions: PermissionDecider,
    ) -> AgentProcessCompletion:
        for call_ordinal, (effect, scope) in enumerate(
            self.asks, start=MINIMUM_PERMISSION_CALL_ORDINAL
        ):
            self.answers.append(
                permissions.decide(
                    PermissionRequest(
                        effect,
                        scope,
                        PermissionCorrelationId.for_call(
                            execution.attempt_id, call_ordinal
                        ),
                    )
                )
            )
        if self.file_requests:
            assert invocation.conversation is not None, (
                "a scenario asking for files needs an executor that opened a conversation"
            )
            for request in self.file_requests:
                self.file_replies.append(invocation.conversation.files.answer(request))
        return self.completion

    def cancel(
        self,
        attempt: AgentAttempt,
        cause: ProviderCancellationCause = ProviderCancellationCause.OPERATOR,
    ) -> Never:
        del attempt, cause
        raise NotImplementedError(_NO_FAKED_SUPERVISION)

    def recover(self, attempt: AgentAttempt) -> Never:
        del attempt
        raise NotImplementedError(_NO_FAKED_SUPERVISION)

    def release(self, attempt: AgentAttempt) -> None:
        del attempt

    def finalize(self, execution: AgentAttemptExecution) -> None:
        del execution


_NO_FAKED_SUPERVISION = (
    "a scenario that stops a live attempt needs the real session, not this fake"
)

NOTHING_IS_PERMITTED = PolicyPermissionDecider(GRANTS_NOTHING)
"""The authority a scenario hands a session it expects to ask nothing."""
