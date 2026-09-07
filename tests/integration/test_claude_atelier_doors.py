"""The Claude atelier-doors executor: its vector, its identity, its gates.

Every proof here is deterministic and unbilled. The billed conformance probe --
a door really fires, a built-in stays imitation-only beside a live MCP server,
no customization returns through safe mode's absence -- is a separate
operator-gated step and deliberately has no test here: an offline test claiming
it would lie.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from atelier2.adapters.claude_subscription import (
    CLAUDE_ATELIER_DOORS_EXECUTOR_KEY,
    CLAUDE_ATELIER_DOORS_OPERATIONAL_IDENTITY,
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_FRAME_BYTES,
    CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY,
    ClaudeAtelierDoorsExecutorFactory,
    ClaudeAtelierDoorsSettings,
    ClaudeExecutableUnsupported,
    ClaudeSubscriptionAuthModeUnsupported,
    attest_atelier_doors_invocation,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    InstanceRefusal,
    InstanceRefused,
    InstanceVerdict,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.host.mcp_tools import MCP_SERVER_NAME, McpToolName
from atelier2.host.serving import ATELIER_DOOR_TOOLS
from atelier2.ports.agent_executions import AgentProcessInvocation
from tests.integration.test_claude_subscription import (
    INTROSPECTING_CLAUDE,
    argument_after,
    launched,
    leased,
    parsing_claude,
    provider_workspace,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_workspace_owner,
    claude_subscription_deployment,
)

_LOOPBACK_SERVICE_URL = "http://127.0.0.1:8422"

# A nontrivial output schema this scenario declares -- its shape does not
# matter to what these tests prove (that a declared schema changes the job,
# never the door grant, and that the seam decodes exactly what it admits);
# it only needs to be a real JSON Schema `_EPISODE_REPORT` below validates
# against.
_SAMPLE_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "required": [
            "answer",
            "started_run_ids",
            "carried_context",
            "carried_context_truncated",
        ],
        "additionalProperties": False,
        "properties": {
            "answer": {"type": "string", "minLength": 1},
            "started_run_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "carried_context": {"type": "string"},
            "carried_context_truncated": {"type": "boolean"},
        },
    },
    sort_keys=True,
    separators=(",", ":"),
).encode()


def doors_deployment(root: Path, name: str, program: str) -> ClaudeAtelierDoorsSettings:
    """One atelier-doors deployment composed the way the serving host does.

    The server name and door tools come from the conductor contract's typed
    owners, and the door command launches this test's interpreter as the stdio
    door -- the same shape `_atelier_doors_settings` composes in production.
    """

    directory = root / name
    directory.mkdir()
    door_command = (
        "/usr/bin/env",
        "python3",
        "-m",
        "atelier2",
        "mcp",
        "--service",
        _LOOPBACK_SERVICE_URL,
    )
    return ClaudeAtelierDoorsSettings(
        claude_subscription_deployment(directory, program),
        MCP_SERVER_NAME,
        tuple(tool.value for tool in ATELIER_DOOR_TOOLS),
        door_command,
    )


def doors_request(
    model: str = "claude-opus-4-6",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
    job: bytes = b"choose a workflow, start it, and report the run",
    maximum_assistant_turns: int | None = None,
    declared_output_schema_bytes: bytes | None = None,
) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), auth_mode)
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.executor_revision,
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("run-conductor")
    revision_hash = WorkflowRevisionHash("3" * 64)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "conduct"),
        run_id,
        revision_hash,
        "conduct",
        ResolvedAgentBinding(AgentRole("conductor"), configuration, auth),
        CLAUDE_ATELIER_DOORS_OPERATIONAL_IDENTITY,
        job,
        maximum_assistant_turns=maximum_assistant_turns,
        declared_output_schema_bytes=declared_output_schema_bytes,
    )


def doors_flags(settings: ClaudeAtelierDoorsSettings) -> tuple[str, ...]:
    """Every flag the real atelier-doors invocation carries."""

    command = (
        ClaudeAtelierDoorsExecutorFactory(settings)
        .open()
        .prepare_process(doors_request())
    )
    return tuple(
        argument for argument in command.arguments if argument.startswith("--")
    )


@pytest.mark.proves("the-doors-vector-admits-exactly-the-granted-doors")
def test_the_doors_vector_admits_exactly_the_granted_doors(tmp_path: Path) -> None:
    """The containment is the vector, and every piece of it is asserted exactly.

    Beyond the gate's link: the allowlist is derived from the door vocabulary's
    typed owner and admits neither a built-in nor either write-shaped door, the
    one MCP server is the serving host's own door command, and `--safe-mode` is
    absent because it was measured to prevent that server from spawning at all.
    """

    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request(model="claude-sonnet-4-6", job=b"start the build")

    command = executor.prepare_process(request)

    allowlist = ",".join(
        f"mcp__{MCP_SERVER_NAME}__{tool.value}" for tool in ATELIER_DOOR_TOOLS
    )
    assert command.arguments == (
        str(settings.deployment.executable),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-sonnet-4-6",
        "--tools=",
        "--allowedTools",
        allowlist,
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        settings.door_mcp_config(),
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
        "--max-turns",
        "6",
    )
    assert "--safe-mode" not in command.arguments
    for tool in (McpToolName.ANSWER_WAIT, McpToolName.PUBLISH_ARTIFACT):
        assert tool.value not in allowlist
    for built_in in ("Bash", "Edit", "Glob", "Grep", "Read", "Write"):
        assert built_in not in allowlist
    door = json.loads(settings.door_mcp_config())
    assert set(door) == {"mcpServers"}
    assert set(door["mcpServers"]) == {MCP_SERVER_NAME}
    assert door["mcpServers"][MCP_SERVER_NAME] == {
        "command": settings.door_command[0],
        "args": list(settings.door_command[1:]),
    }
    environment = dict(command.environment)
    state_directory = Path(environment.pop("CLAUDE_CONFIG_DIR"))
    # The private, disposable directory this call alone was handed -- never
    # the operator's own directory (issue #993).
    assert state_directory != settings.deployment.credential_directory
    assert state_directory.is_dir()
    assert environment == {
        "PATH": settings.deployment.search_path,
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
    }
    assert command.standard_output_frame_bytes == CLAUDE_SUBSCRIPTION_FRAME_BYTES
    assert all(argument for argument in command.arguments)

    workspace = provider_workspace(tmp_path)
    result = executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )
    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][1:] == list(command.arguments[1:])
    assert observed["job"] == "start the build"
    assert "HOME" not in observed["environment"]
    assert observed["environment"]["CLAUDE_CONFIG_DIR"] == str(state_directory)

    executor.release_credential_channel(command)
    assert not state_directory.exists()


def test_a_schema_bearing_doors_call_grants_no_tool_for_the_schema(
    tmp_path: Path,
) -> None:
    """A declared schema changes this vector's job, never its door grant (#1188).

    The schema used to reach Anthropic as a synthesized tool's `input_schema`,
    so a schema-bearing episode was granted a tool beside its doors -- and a
    schema carrying a top-level `allOf` refused the whole call there. It closes
    the job now, so this allowlist is the doors alone whatever a node declares.
    """

    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    command = executor.prepare_process(
        doors_request(declared_output_schema_bytes=_SAMPLE_OUTPUT_SCHEMA)
    )

    assert "--tools=" in command.arguments
    assert argument_after(command.arguments, "--allowedTools") == ",".join(
        settings.allowed_door_tools
    )
    assert not any(
        argument.startswith("--json-schema") for argument in command.arguments
    )
    assert command.standard_input is not None
    assert command.standard_input.endswith(_SAMPLE_OUTPUT_SCHEMA)
    executor.release_credential_channel(command)
    executor.close()


def test_a_pinned_budget_replaces_the_default_turn_bound(tmp_path: Path) -> None:
    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()

    default_command = executor.prepare_process(doors_request())
    pinned_command = executor.prepare_process(doors_request(maximum_assistant_turns=3))

    assert argument_after(default_command.arguments, "--max-turns") == "6"
    assert argument_after(pinned_command.arguments, "--max-turns") == "3"


def test_a_non_subscription_profile_reaches_no_door_bearing_process(
    tmp_path: Path,
) -> None:
    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()

    with pytest.raises(ClaudeSubscriptionAuthModeUnsupported, match="subscription"):
        executor.prepare_process(doors_request(auth_mode=AuthMode.API_KEY))


def test_the_factory_offers_its_own_identity_beside_both_siblings(
    tmp_path: Path,
) -> None:
    """A third operation of one provider, not a revision of either sibling."""

    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    factory = ClaudeAtelierDoorsExecutorFactory(settings)

    assert factory.key == CLAUDE_ATELIER_DOORS_EXECUTOR_KEY
    assert factory.key.provider_id == CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id
    assert factory.key.executor_revision not in {
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
    }
    assert factory.declared_capabilities == frozenset(
        {AgentExecutionCapability.HEADLESS_WITH_TOOLS}
    )
    assert factory.open().close() is None


def test_the_doors_executors_private_config_directory_outlives_neither_a_completion_nor_a_close(
    tmp_path: Path,
) -> None:
    """A third implementation of the same private-directory lifecycle, proved
    independently: this executor's `prepare_process`/`release_credential_channel`/
    `close` are their own code, not inherited from either sibling."""

    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request()
    workspace = provider_workspace(tmp_path)

    command = executor.prepare_process(request)
    state_directory = Path(dict(command.environment)["CLAUDE_CONFIG_DIR"])
    assert state_directory != settings.deployment.credential_directory
    assert state_directory.exists()

    executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )
    executor.release_credential_channel(command)

    assert not state_directory.exists()

    second = executor.prepare_process(request)
    abandoned_directory = Path(dict(second.environment)["CLAUDE_CONFIG_DIR"])
    assert abandoned_directory.exists()

    executor.close()

    assert not abandoned_directory.exists()


def test_settings_refuse_an_empty_door_grant(tmp_path: Path) -> None:
    """A doors executor with no doors, no server or no command is not a deployment."""

    directory = tmp_path / "deployment"
    directory.mkdir()
    deployment = claude_subscription_deployment(directory, INTROSPECTING_CLAUDE)
    door_command = ("/usr/bin/env", "python3", "-m", "atelier2", "mcp")

    with pytest.raises(ValueError, match="door tools"):
        ClaudeAtelierDoorsSettings(deployment, MCP_SERVER_NAME, (), door_command)
    with pytest.raises(ValueError, match="server name"):
        ClaudeAtelierDoorsSettings(
            deployment, " ", (McpToolName.LIST_WORKFLOWS.value,), door_command
        )
    with pytest.raises(ValueError, match="door command"):
        ClaudeAtelierDoorsSettings(
            deployment, MCP_SERVER_NAME, (McpToolName.LIST_WORKFLOWS.value,), ()
        )


def test_an_executable_that_starts_this_exact_invocation_is_attested(
    tmp_path: Path,
) -> None:
    reference = doors_deployment(tmp_path, "reference", INTROSPECTING_CLAUDE)
    settings = doors_deployment(
        tmp_path, "deployment", parsing_claude(doors_flags(reference))
    )

    assert attest_atelier_doors_invocation(settings) is None


def test_an_executable_missing_any_flag_of_this_invocation_is_refused_by_that_flag(
    tmp_path: Path,
) -> None:
    """Every flag of the vector is a containment decision, so every one is probed."""

    reference = doors_deployment(tmp_path, "reference", INTROSPECTING_CLAUDE)
    flags = doors_flags(reference)

    assert flags
    for missing in flags:
        settings = doors_deployment(
            tmp_path,
            f"without{flags.index(missing)}",
            parsing_claude(flag for flag in flags if flag != missing),
        )

        with pytest.raises(ClaudeExecutableUnsupported, match=re.escape(missing)):
            attest_atelier_doors_invocation(settings)


def test_an_executable_that_never_names_an_unknown_flag_cannot_be_attested(
    tmp_path: Path,
) -> None:
    """Without the control, "said nothing" and "has nothing to say" look alike."""

    reference = doors_deployment(tmp_path, "reference", INTROSPECTING_CLAUDE)
    settings = doors_deployment(
        tmp_path,
        "deployment",
        parsing_claude(doors_flags(reference), refuses_unknown=False),
    )

    with pytest.raises(ClaudeExecutableUnsupported, match="no release can know"):
        attest_atelier_doors_invocation(settings)


# The shape #656/#661 measured `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` leaving in the
# invocation's own working directory before it can launch the door child under
# bubblewrap: empty bind-mount targets neither this executor nor the model asked
# for. A subset of the exact list #1166 then measured
# (`_SUBPROCESS_ENVIRONMENT_SCRUB_RESIDUE`), spelled here rather than imported
# so this stays a fake CLI's own behaviour and not the sweep agreeing with
# itself.
DOORS_SCRUB_RESIDUE_FILES = (
    Path(".env"),
    Path(".env.local"),
    Path("package.json"),
    Path("yarn.lock"),
    Path(".npmrc"),
)
DOORS_SCRUB_RESIDUE_DIRECTORIES = (
    Path(".claude/commands"),
    Path(".claude/agents"),
    Path("node_modules/.bin"),
)

RESIDUE_LEAVING_CLAUDE = (
    "import json, os, sys\n"
    "sys.stdin.buffer.read()\n"
    f"for relative in {tuple(str(path) for path in DOORS_SCRUB_RESIDUE_FILES)!r}:\n"
    "    open(os.path.join(os.getcwd(), relative), 'a').close()\n"
    "for relative in "
    f"{tuple(str(path) for path in DOORS_SCRUB_RESIDUE_DIRECTORIES)!r}:\n"
    "    os.makedirs(os.path.join(os.getcwd(), relative))\n"
    "json.dump(\n"
    "    {'type': 'result', 'is_error': False, 'result': json.dumps({'ran': True})},\n"
    "    sys.stdout,\n"
    ")\n"
)
"""A fake CLI standing where a real Claude Code under the subprocess-env scrub
would stand: it leaves the measured residue shape in its own cwd before it
answers, the same way the real CLI leaves it before it manages to spawn the
door child."""


def test_a_doors_attempt_leaves_none_of_the_scrub_residue_in_its_workspace(
    tmp_path: Path,
) -> None:
    """The residue is gone when the call ends, not when the lease is retired.

    `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is genuine containment for the door
    child this executor expects (see its class docstring), and the CLI's own
    bind-mount preparation is not this module's to change. What this module
    does own is the working directory the CLI was started in -- and where a
    project is pinned, that directory is the candidate the atelier keeps and
    pushes, read long before the lease is released (#1166). So decoding the
    answer takes the residue back, and the lease still retires whatever is
    left the way it always did.
    """

    settings = doors_deployment(tmp_path, "deployment", RESIDUE_LEAVING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request()
    command = executor.prepare_process(request)
    state_directory = Path(dict(command.environment)["CLAUDE_CONFIG_DIR"])

    workspaces = agent_workspace_owner(tmp_path)
    try:
        attempt_id = agent_attempt_execution(request).attempt_id
        lease = workspaces.acquire(attempt_id)
        completion = launched(command, lease.working_directory)
        result = executor.decode_process_completion(
            AgentProcessInvocation(command, lease), completion
        )

        assert isinstance(result, AgentExecutionResult)
        for relative in DOORS_SCRUB_RESIDUE_FILES + DOORS_SCRUB_RESIDUE_DIRECTORIES:
            assert not (lease.working_directory / relative).exists()
        assert not any(lease.working_directory.iterdir())

        workspaces.release(attempt_id)
    finally:
        workspaces.close()
        executor.close()

    assert not lease.working_directory.exists()
    assert not state_directory.exists()


# The report every fake episode below answers with. Its field names are this
# scenario's, not a second owner's: what makes them right is that
# `_SAMPLE_OUTPUT_SCHEMA` -- the schema the request declares -- admits the
# value, which every assertion here goes through.
_EPISODE_REPORT = {
    "answer": "Started the tidy workflow; run-tidy-1 is running.",
    "started_run_ids": ["run-tidy-1"],
    "carried_context": "Started run-tidy-1 from the tidy workflow request.",
    "carried_context_truncated": False,
}

# The four result-text shapes one identical brief really came back in (#663,
# live 25.08.), and the terminal `result` text is again the whole answer this
# executor hands on (#1188), so which bytes of it are the declared value is
# what decides whether an episode ends as a report.
_OBSERVED_ANSWER_SHAPES = (
    "{report}",
    "{report}\n",
    "Here is the report:\n\n{report}",
    "```json\n{report}\n```",
)


def cycling_claude(shapes: tuple[str, ...]) -> str:
    """A fake CLI that answers one report through these wrappers in turn.

    A fake that answered identically every time would prove nothing about a
    provider whose defect is that it does not: the counter beside the program
    is what makes ten identical episodes really meet the varying answer one
    identical brief produced live.
    """

    return (
        "import json, os, sys\n"
        f"report = json.dumps({_EPISODE_REPORT!r})\n"
        f"shapes = {shapes!r}\n"
        "counter = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'answered')\n"
        "answered = int(open(counter).read()) if os.path.exists(counter) else 0\n"
        "open(counter, 'w').write(str(answered + 1))\n"
        "sys.stdin.buffer.read()\n"
        "json.dump(\n"
        "    {\n"
        "        'type': 'result',\n"
        "        'is_error': False,\n"
        "        'result': shapes[answered % len(shapes)].replace('{report}', report),\n"
        "    },\n"
        "    sys.stdout,\n"
        ")\n"
    )


def answering_claude(answer: str) -> str:
    """A fake CLI whose whole session is this one terminal answer."""

    return (
        "import json, sys\n"
        "sys.stdin.buffer.read()\n"
        f"json.dump({{'type': 'result', 'is_error': False, 'result': {answer!r}}}, "
        "sys.stdout)\n"
    )


def episode_output(settings: ClaudeAtelierDoorsSettings, workspace: Path) -> bytes:
    """One whole episode: the real vector launched, and its real decode."""

    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request(declared_output_schema_bytes=_SAMPLE_OUTPUT_SCHEMA)
    command = executor.prepare_process(request)
    outcome = executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )
    executor.release_credential_channel(command)
    assert isinstance(outcome, AgentExecutionResult), outcome
    return outcome.output_bytes


def report_verdict(output: bytes) -> InstanceVerdict:
    """What the output seam makes of these bytes, through its own owner."""

    schema = read_schema_document(_SAMPLE_OUTPUT_SCHEMA)
    assert isinstance(schema, SchemaAccepted), schema
    return read_instance_document(output, schema)


@pytest.mark.proves("an-episode-answers-the-value-its-schema-declared")
def test_ten_identical_episodes_all_answer_a_value_the_report_schema_admits(
    tmp_path: Path,
) -> None:
    """The defect this closes: one brief, one schema, and a coin flip between them.

    Ten episodes here meet every result-text wrapper that was observed. Each
    returns the same provider-native value, so narration cannot decide whether
    the output seam accepts the run.
    """

    settings = doors_deployment(
        tmp_path, "cycling", cycling_claude(_OBSERVED_ANSWER_SHAPES)
    )
    workspace = provider_workspace(tmp_path)

    verdicts = [episode_output(settings, workspace) for _ in range(10)]

    assert [report_verdict(output) for output in verdicts] == [
        InstanceAccepted(_EPISODE_REPORT)
    ] * 10


@pytest.mark.proves("an-episode-answering-no-such-value-is-still-refused")
@pytest.mark.parametrize(
    ("answer", "refusal"),
    [
        pytest.param(
            '{"answer": "done"}',
            InstanceRefusal.SCHEMA_VIOLATED,
            id="an object missing required fields",
        ),
        pytest.param(
            "[]",
            InstanceRefusal.SCHEMA_VIOLATED,
            id="an array instead of a report object",
        ),
        pytest.param(
            "I started the tidy workflow.",
            InstanceRefusal.INSTANCE_NOT_JSON,
            id="prose carrying no document at all",
        ),
    ],
)
def test_an_episode_carrying_no_declared_value_is_refused_rather_than_narrowed(
    tmp_path: Path, answer: str, refusal: InstanceRefusal
) -> None:
    """Fail loud: narrowing proposes, the output seam judges, nothing repairs."""

    settings = doors_deployment(tmp_path, "refusing", answering_claude(answer))
    workspace = provider_workspace(tmp_path)

    verdict = report_verdict(episode_output(settings, workspace))

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is refusal


def test_an_episode_whose_node_declared_no_schema_keeps_the_answer_it_was_given(
    tmp_path: Path,
) -> None:
    """Narrowing is the declared schema's, so a node without one is untouched."""

    settings = doors_deployment(tmp_path, "unbound", answering_claude("plain words"))
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request()
    command = executor.prepare_process(request)
    workspace = provider_workspace(tmp_path)

    outcome = executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )

    assert isinstance(outcome, AgentExecutionResult)
    assert outcome.output_bytes == b"plain words"
