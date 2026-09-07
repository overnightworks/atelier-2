"""What one Claude invocation leaves standing in the workspace it was started in.

Before this CLI starts anything under bubblewrap it prepares bind-mount targets
in its own working directory, and that directory is the attempt's lease. Where a
project is pinned, the lease *is* the candidate: it is read against the pin,
kept, and pushed as an Atelier commit. So what these tests ask is the one
question that decides whether an attempt tells the truth about its work -- after
a call that changed nothing, does the tree still equal the pin (#1166, #1156)?

The residue below is measured, not assumed: it is what the pinned Claude 2.1.221
really created in an empty directory under this module's own vector, taken
unbilled with no credential in reach.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from atelier2.adapters.candidate_store import GitCandidateTreeStore
from atelier2.adapters.claude_subscription import (
    ClaudeAtelierDoorsExecutorFactory,
    ClaudeAtelierDoorsSettings,
    ClaudeSubscriptionExecutorFactory,
    ClaudeWorkspaceToolExecutorFactory,
)
from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.host.mcp_tools import MCP_SERVER_NAME
from atelier2.host.serving import ATELIER_DOOR_TOOLS
from atelier2.ports.agent_executions import (
    AgentExecutorV2,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.candidate_store import LeasedWorkingTree
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    claude_subscription_deployment,
    leased_directory_identity,
)
from tests.scenarios.projects import git_project, write_into_checkout

WHAT_THE_SCRUB_PREPARES = """\
.claude/agents/
.claude/commands/
node_modules/.bin/
.env
.env.development
.env.development.local
.env.local
.env.production
.env.production.local
.env.test
.env.test.local
.gitmodules
.npmrc
.yarnrc
.yarnrc.yml
bunfig.toml
package.json
package-lock.json
pnpm-lock.yaml
yarn.lock
"""
"""Every entry the measured scrub preparation creates, a directory ending in `/`.

Spelled out here rather than read from the executor's own list: this is the
measurement the sweep is answerable to, so a list that drifted away from what
the CLI really does has to fail here instead of agreeing with itself.
"""

A_CLAUDE_THAT_IS_NEVER_STARTED = "raise SystemExit(0)\n"
"""These tests decode a canned answer; no invocation is ever launched."""

THE_PINNED_PROJECT: Mapping[str, str] = {
    "README.md": "the project the attempt was pinned to\n",
    # A name the scrub also prepares, carrying content: the CLI leaves an entry
    # that already stands exactly as it is (measured), and so must the sweep.
    "package.json": '{"name": "the-pinned-project"}\n',
}


def _tool_free_executor(directory: Path) -> AgentExecutorV2:
    return ClaudeSubscriptionExecutorFactory(_deployment(directory)).open()


def _workspace_tool_executor(directory: Path) -> AgentExecutorV2:
    return ClaudeWorkspaceToolExecutorFactory(_deployment(directory)).open()


def _atelier_doors_executor(directory: Path) -> AgentExecutorV2:
    return ClaudeAtelierDoorsExecutorFactory(
        ClaudeAtelierDoorsSettings(
            _deployment(directory),
            MCP_SERVER_NAME,
            tuple(tool.value for tool in ATELIER_DOOR_TOOLS),
            (
                sys.executable,
                "-m",
                "atelier2",
                "mcp",
                "--service",
                "http://127.0.0.1:8422",
            ),
        )
    ).open()


EVERY_CLAUDE_EXECUTOR = pytest.mark.parametrize(
    "open_executor",
    [
        pytest.param(_tool_free_executor, id="the tool-free heartbeat"),
        pytest.param(_workspace_tool_executor, id="the workspace-tool builder"),
        pytest.param(_atelier_doors_executor, id="the atelier doors"),
    ],
)


def _deployment(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    return claude_subscription_deployment(directory, A_CLAUDE_THAT_IS_NEVER_STARTED)


def _an_answer() -> bytes:
    """The one line a finished `--output-format stream-json` call ends with."""

    return (
        json.dumps({"type": "result", "is_error": False, "result": "done"}).encode(
            "utf-8"
        )
        + b"\n"
    )


UNWRITABLE_DIRECTORY = 0o500
"""Readable and enterable, but nothing in it can be removed -- the sweep's wall."""

WRITABLE_AGAIN = 0o700
"""Given back before the tree is read, so the scenario ends where it started."""


def _what_the_attempt_left(
    tmp_path: Path,
    open_executor: Callable[[Path], AgentExecutorV2],
    wrote: Mapping[str, str],
    sealed: tuple[str, ...] = (),
) -> LeasedWorkingTree:
    """Run one Claude invocation's decode over a pinned workspace, and read the tree.

    The CLI is not started -- what a started one does to that directory is what
    is simulated: the measured scrub preparation, plus whatever this scenario
    says the model itself wrote. The tree comes back from the project's own
    candidate store, the same reading a run makes before it pays for anything.

    `sealed` names lease directories this scenario makes unwritable while the
    decode runs: the workspace the sweep cannot take an entry back out of.
    """

    root = tmp_path / "project"
    pin = git_project(root, THE_PINNED_PROJECT)
    execution = agent_attempt_execution(agent_execution_request_v2())
    lease = leased_directory_identity(execution.attempt_id, tmp_path / "workspace")
    LocalGitProjectSource(root).materialize(pin, lease)
    _prepare_scrub_targets(lease.working_directory)
    write_into_checkout(lease.working_directory, wrote)

    for name in sealed:
        (lease.working_directory / name).chmod(UNWRITABLE_DIRECTORY)

    executor = open_executor(tmp_path / "deployment")
    command = executor.prepare_process(execution.request)
    try:
        decoded = executor.decode_process_completion(
            AgentProcessInvocation(command, lease),
            AgentProcessCompletion(0, _an_answer(), b""),
        )
    finally:
        for name in sealed:
            (lease.working_directory / name).chmod(WRITABLE_AGAIN)
        executor.release_credential_channel(command)
        executor.close()
    assert isinstance(decoded, AgentExecutionResult)

    database_path = tmp_path / "runtime" / "atelier.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return GitCandidateTreeStore(root, database_path).written(pin, lease)


def _prepare_scrub_targets(working_directory: Path) -> None:
    """Create every measured bind-mount target that is not already there."""

    for entry in WHAT_THE_SCRUB_PREPARES.splitlines():
        target = working_directory / entry.rstrip("/")
        if target.exists():
            continue
        if entry.endswith("/"):
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()


@EVERY_CLAUDE_EXECUTOR
def test_a_claude_call_that_changed_nothing_leaves_the_tree_equal_to_the_pin(
    tmp_path: Path, open_executor: Callable[[Path], AgentExecutorV2]
) -> None:
    """The scrub's placeholders are not the attempt's work, and do not read as it.

    Without the sweep this tree carries seventeen empty files and five empty
    directories nobody wrote, which is what let live pass 5 pay for a
    verification and push a commit of placeholders (#1166).
    """

    written = _what_the_attempt_left(tmp_path, open_executor, {})

    assert written.tree == written.pin.tree


@EVERY_CLAUDE_EXECUTOR
def test_what_the_call_really_wrote_survives_the_sweep(
    tmp_path: Path, open_executor: Callable[[Path], AgentExecutorV2]
) -> None:
    """Only an empty entry of the measured list goes; everything else stays.

    Three shapes at once, because all three are ways a real change hides behind
    a scrub name: a file the model wrote, a file the pin already carried under
    one of those names, and a directory the scrub prepared that the model then
    filled.
    """

    written = _what_the_attempt_left(
        tmp_path,
        open_executor,
        {
            "docs/product/interfaces.md": "what the builder was asked to change\n",
            ".claude/commands/one.md": "a command the model wrote\n",
        },
    )

    assert written.tree != written.pin.tree
    changes = (
        GitCandidateTreeStore(
            tmp_path / "project", tmp_path / "runtime" / "atelier.sqlite"
        )
        .changes(written)
        .read
    )
    assert b"docs/product/interfaces.md" in changes
    assert b".claude/commands/one.md" in changes
    assert b"package.json" not in changes
    assert b".env" not in changes
    assert b"yarn.lock" not in changes


def test_an_entry_the_sweep_cannot_remove_is_left_standing_and_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One unremovable placeholder costs its own line, never the whole attempt.

    A `.claude` the pin or the CLI left unwritable is what a raised `OSError`
    would travel out of the decode on, stranding the attempt before any ending
    was recorded -- the repository's own unrecoverable state, paid for an empty
    directory. So the entry stays, the operator is told which one, and the rest
    of the residue is still taken back.
    """

    with caplog.at_level(logging.WARNING, logger="atelier2"):
        written = _what_the_attempt_left(
            tmp_path, _workspace_tool_executor, {}, sealed=(".claude",)
        )

    workspace = tmp_path / "workspace"
    assert (workspace / ".claude" / "agents").is_dir()
    assert not (workspace / ".env").exists()
    assert written.tree == written.pin.tree
    left = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and getattr(record, "event", None) == "claude_scrub_residue_entry_left"
    ]
    assert {getattr(record, "entry", None) for record in left} == {
        ".claude/agents",
        ".claude/commands",
    }
