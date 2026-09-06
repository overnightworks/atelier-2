from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.agent_workspaces import (
    SCRATCH_ROOT_MODE,
    AgentAttemptWorkspaceRefused,
    AgentScratchRootRefused,
    LocalAgentAttemptWorkspaceOwner,
)
from atelier2.adapters.claude_subscription import ClaudeSubscriptionExecutorFactory
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import PRODUCT_TABLE_NAMES, metadata
from atelier2.adapters.leased_directory import (
    LeasedDirectoryChanged,
    entered_leased_directory,
)
from atelier2.application.cancel_agent_attempt import (
    continue_agent_attempt_cancellation,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
)
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptClaimedByThisCall,
    AgentAttemptPossiblyRan,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentExecutionFailure,
    AgentExecutorV2,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    AgentProcessOwnerNotLocal,
    PrintModeExecutor,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_claude_subscription import INTROSPECTING_CLAUDE
from tests.scenarios.agents import (
    NOTHING_IS_PERMITTED,
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
    agent_scratch_root,
    agent_workspace_owner,
    claude_subscription_attempt,
    claude_subscription_deployment,
    claude_subscription_runtime,
    runtime_workspace_owner,
    workspace_files_nobody_opens,
)

# What one real tool-free `claude -p` left behind in its workspace, measured on
# claude 2.1.221 and recorded in issue #29, comment 5298966837, which owns this
# evidence. It is an observation, not a fixed count: a CLI that writes a
# different set is a changed observation to report, never a fixture to adjust
# quietly. Cleanup is proved against whatever this set is, so the proof does
# not depend on how many names it holds.
OBSERVED_PROVIDER_FILES = (
    ".env",
    ".env.local",
    ".env.development",
    ".env.development.local",
    ".env.production",
    ".env.production.local",
    ".env.test",
    ".env.test.local",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    "bunfig.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    ".gitmodules",
)
OBSERVED_PROVIDER_DIRECTORIES = (
    "node_modules/.bin",
    ".claude/agents",
    ".claude/commands",
)
PROVIDER_LINK_NAME = "escape"
"""The symbolic link a provider leaves pointing at something it does not own."""

PROVIDER_TREE_DEPTH_BEYOND_RECURSION = 1_100
"""A nesting depth past the interpreter's own recursion limit: legal for a provider."""

_PROVIDER_MATERIALIZES_ITS_WORKSPACE = """
import os, sys
from pathlib import Path

sentinel, return_code, directories, files = sys.argv[1:5]
for name in directories.split(","):
    Path(name).mkdir(parents=True)
for name in files.split(","):
    Path(name).write_bytes(b"")
Path(sys.argv[5]).symlink_to(sentinel)
os.write(1, b'"done"')
raise SystemExit(int(return_code))
"""

_PROVIDER_WAITS_FOR_ITS_OWN_END = """
import sys, time
from pathlib import Path

Path(sys.argv[1]).write_bytes(b"")
Path(sys.argv[2]).touch()
time.sleep(60)
"""


def materializing_command(sentinel: Path, return_code: int = 0) -> AgentProcessCommand:
    """One provider command that reproduces the observed write set and a link."""

    return AgentProcessCommand(
        (
            sys.executable,
            "-c",
            _PROVIDER_MATERIALIZES_ITS_WORKSPACE,
            str(sentinel),
            str(return_code),
            ",".join(OBSERVED_PROVIDER_DIRECTORIES),
            ",".join(OBSERVED_PROVIDER_FILES),
            PROVIDER_LINK_NAME,
        ),
        standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
    )


@dataclass
class MaterializingExecutor(PrintModeExecutor):
    """A provider that writes into its workspace exactly as the measured CLI did."""

    sentinel: Path
    return_code: int = 0
    decodes: int = 0

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        return materializing_command(self.sentinel, self.return_code)

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        self.decodes += 1
        if completion.return_code != 0:
            return AgentExecutionFailure(
                AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
            )
        return AgentExecutionResult(completion.standard_output)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return


@dataclass
class WaitingExecutor(PrintModeExecutor):
    """A provider that writes one file into its workspace and then hangs."""

    ready: Path

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(
            (
                sys.executable,
                "-c",
                _PROVIDER_WAITS_FOR_ITS_OWN_END,
                ".env",
                str(self.ready),
            ),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        return AgentExecutionResult(completion.standard_output)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return


@dataclass
class WatchingWorkspaceOwner:
    """One real owner, recording the durable attempt state at every decision."""

    owner: LocalAgentAttemptWorkspaceOwner
    store: DbosAgentAttemptStore
    observed: list[tuple[str, AgentAttemptState | None]] = field(default_factory=list)
    acquired: list[AgentAttemptId] = field(default_factory=list)
    watched: AgentAttemptId | None = None

    def preflight(self) -> None:
        self.observed.append(("preflight", self._durable_state()))
        self.owner.preflight()

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        self.observed.append(("acquire", self._durable_state()))
        lease = self.owner.acquire(attempt_id)
        self.acquired.append(attempt_id)
        return lease

    def release(self, attempt_id: AgentAttemptId) -> None:
        self.observed.append(("release", self._durable_state()))
        self.owner.release(attempt_id)

    def _durable_state(self) -> AgentAttemptState | None:
        return None if self.watched is None else self.store.load(self.watched).state


SENTINEL_FILE_NAME = "the-operators-own-file"


def sentinel_directory(root: Path) -> Path:
    """A tree outside every workspace, which no cleanup may follow a link into.

    A provider leaves a symbolic link to it inside its workspace, so a cleanup
    that followed links would empty this directory instead of only its own.
    """

    sentinel = root / "outside-every-workspace"
    sentinel.mkdir()
    (sentinel / SENTINEL_FILE_NAME).write_bytes(b"never removed by any attempt")
    return sentinel


def sentinel_survived(sentinel: Path) -> bool:
    return (
        sentinel / SENTINEL_FILE_NAME
    ).read_bytes() == b"never removed by any attempt"


def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def workspace_names(root: Path) -> set[str]:
    """Every name the scratch root holds, so nothing left behind stays invisible."""

    return {entry.name for entry in root.iterdir()}


def leased_directories(root: Path) -> set[str]:
    return {entry.name for entry in root.iterdir() if entry.is_dir()}


def open_descriptors() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


@pytest.mark.proves("a-lost-claim-leaves-no-directory-behind")
def test_a_workspace_is_created_only_once_this_call_holds_the_durable_claim(
    tmp_path: Path,
) -> None:
    """The scratch root is attested while nothing is claimed, and created after."""

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "lease/ordering"))
        store = DbosAgentAttemptStore(runtime.engine)
        workspaces = WatchingWorkspaceOwner(
            runtime_workspace_owner(runtime), store, watched=execution.attempt_id
        )

        outcome = execute_agent_attempt(
            execution,
            MaterializingExecutor(sentinel_directory(tmp_path)),
            store,
            runtime.agent_process_supervisor,
            workspaces,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

        assert isinstance(outcome, AgentAttemptSucceeded)
        assert workspaces.observed == [
            ("preflight", AgentAttemptState.PREPARED),
            ("acquire", AgentAttemptState.LAUNCH_ARMED),
            ("release", AgentAttemptState.SUCCEEDED),
        ]
    finally:
        runtime.close()


@pytest.mark.proves("a-lost-claim-leaves-no-directory-behind")
def test_thirty_two_racing_callers_create_exactly_one_workspace(
    tmp_path: Path,
) -> None:
    """Losing the claim leaves no inode: only the winner ever creates one."""

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "lease/race"))
        store = DbosAgentAttemptStore(runtime.engine)
        owner = runtime_workspace_owner(runtime)
        workspaces = WatchingWorkspaceOwner(owner, store)
        executor = MaterializingExecutor(sentinel_directory(tmp_path))
        outcomes: list[object] = []
        barrier = threading.Barrier(32)

        def claim() -> None:
            barrier.wait()
            outcomes.append(
                execute_agent_attempt(
                    execution,
                    executor,
                    store,
                    runtime.agent_process_supervisor,
                    workspaces,
                    permissions=GRANTS_NOTHING,
                    workspace_files=workspace_files_nobody_opens,
                )
            )

        callers = [threading.Thread(target=claim) for _ in range(32)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=30)

        assert workspaces.acquired == [execution.attempt_id]
        assert sum(isinstance(value, AgentAttemptSucceeded) for value in outcomes) >= 1
        assert all(
            isinstance(value, (AgentAttemptSucceeded, AgentAttemptPossiblyRan))
            for value in outcomes
        )
        assert workspace_names(owner.scratch_root) == set()
    finally:
        runtime.close()


@pytest.mark.proves("every-attempt-runs-in-a-blank-directory-of-its-own")
def test_an_attempt_and_its_replacement_lease_directories_of_their_own(
    tmp_path: Path,
) -> None:
    """Each directory is named by the attempt identity, which carries the ordinal."""

    owner = agent_workspace_owner(tmp_path)
    node_execution_id = NodeExecutionId.for_node(
        RunId("lease/naming"), WorkflowRevisionHash("4" * 64), "build"
    )
    request_hash = AgentExecutionRequestHash("5" * 64)
    attempt_ids = tuple(
        AgentAttemptId.for_execution(node_execution_id, request_hash, ordinal)
        for ordinal in (1, 2)
    )

    leases = tuple(owner.acquire(attempt_id) for attempt_id in attempt_ids)

    assert [lease.working_directory for lease in leases] == [
        owner.scratch_root / attempt_id.value for attempt_id in attempt_ids
    ]
    assert all(lease.working_directory.is_dir() for lease in leases)
    assert leased_directories(owner.scratch_root) == {
        attempt_id.value for attempt_id in attempt_ids
    }
    with pytest.raises(AgentAttemptWorkspaceRefused, match="already exists"):
        owner.acquire(attempt_ids[0])


@pytest.mark.proves("a-lost-claim-leaves-no-directory-behind")
def test_two_callers_acquiring_one_attempt_leave_exactly_one_directory(
    tmp_path: Path,
) -> None:
    """A race after the preflight loses at the atomic no-replace creation."""

    owner = agent_workspace_owner(tmp_path)
    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId.for_node(
            RunId("lease/concurrent"), WorkflowRevisionHash("4" * 64), "build"
        ),
        AgentExecutionRequestHash("5" * 64),
    )
    barrier = threading.Barrier(2)
    leases: list[AgentAttemptWorkspaceLease] = []
    refusals: list[BaseException] = []

    def acquire() -> None:
        barrier.wait()
        try:
            leases.append(owner.acquire(attempt_id))
        except AgentAttemptWorkspaceRefused as refusal:
            refusals.append(refusal)

    callers = [threading.Thread(target=acquire) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=10)

    assert len(leases) == 1
    assert len(refusals) == 1
    assert leased_directories(owner.scratch_root) == {attempt_id.value}


def symlinked_root(root: Path) -> Path:
    real = root / "real"
    real.mkdir(mode=SCRATCH_ROOT_MODE)
    link = root / "linked"
    link.symlink_to(real)
    return link


def root_below_a_symlink(root: Path) -> Path:
    real = root / "real"
    (real / "scratch").mkdir(mode=SCRATCH_ROOT_MODE, parents=True)
    (root / "linked").symlink_to(real)
    return root / "linked" / "scratch"


def root_in_a_git_worktree(root: Path) -> Path:
    """A checkout-shaped parent, never the temp root itself.

    The marker is a real git directory (`HEAD` present), nested under a
    subdirectory this test owns. An empty `.git` on `root` itself would, if
    `root` were ever the process temp directory, leave `/tmp/.git` behind and
    make every later scratch root under `/tmp` look like a worktree.
    """

    checkout = root / "checkout"
    git_directory = checkout / ".git"
    git_directory.mkdir(parents=True)
    (git_directory / "HEAD").touch()
    scratch = checkout / "scratch"
    scratch.mkdir(mode=SCRATCH_ROOT_MODE)
    return scratch


def root_beside_a_git_file(root: Path) -> Path:
    scratch = root / "scratch"
    scratch.mkdir(mode=SCRATCH_ROOT_MODE)
    (scratch / ".git").write_text("gitdir: /elsewhere", encoding="utf-8")
    return scratch


def group_readable_root(root: Path) -> Path:
    scratch = root / "scratch"
    scratch.mkdir(mode=0o750)
    return scratch


def world_writable_root(root: Path) -> Path:
    scratch = root / "scratch"
    scratch.mkdir(mode=0o777)
    return scratch


def root_holding_a_foreign_entry(root: Path) -> Path:
    scratch = root / "scratch"
    scratch.mkdir(mode=SCRATCH_ROOT_MODE)
    (scratch / "notes.txt").write_text("somebody else's", encoding="utf-8")
    return scratch


def root_holding_a_named_symlink(root: Path) -> Path:
    scratch = root / "scratch"
    scratch.mkdir(mode=SCRATCH_ROOT_MODE)
    (scratch / ("a" * 64)).symlink_to(root)
    return scratch


def absent_root(root: Path) -> Path:
    return root / "never-created"


@pytest.mark.parametrize(
    ("build_root", "refusal"),
    [
        pytest.param(symlinked_root, "symbolic link", id="the root is a link"),
        pytest.param(root_below_a_symlink, "symbolic link", id="a parent is a link"),
        pytest.param(root_in_a_git_worktree, "git worktree", id="a parent is a repo"),
        pytest.param(root_beside_a_git_file, "git worktree", id="the root is a repo"),
        pytest.param(group_readable_root, "mode 700", id="the group may read it"),
        pytest.param(world_writable_root, "mode 700", id="the world may write it"),
        pytest.param(
            root_holding_a_foreign_entry, "no attempt workspace", id="a foreign entry"
        ),
        pytest.param(
            root_holding_a_named_symlink,
            "no attempt workspace",
            id="a link named like an attempt",
        ),
        pytest.param(absent_root, "existing directory", id="no root exists"),
    ],
)
@pytest.mark.proves("an-unusable-scratch-root-is-refused-before-the-server-exists")
def test_an_unusable_scratch_root_is_refused_without_mutating_it(
    tmp_path: Path, build_root: Callable[[Path], Path], refusal: str
) -> None:
    scratch_root = build_root(tmp_path)
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    with pytest.raises(AgentScratchRootRefused, match=refusal):
        LocalAgentAttemptWorkspaceOwner(scratch_root)

    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


@pytest.mark.proves("an-empty-git-directory-above-a-scratch-root-is-not-a-worktree")
def test_an_empty_git_directory_above_a_scratch_root_is_not_a_worktree(
    tmp_path: Path,
) -> None:
    """A leftover empty `.git` on `/tmp` must not poison every pytest scratch root."""

    (tmp_path / ".git").mkdir()
    owner = agent_workspace_owner(tmp_path / "below")
    owner.close()


@pytest.mark.proves("an-empty-git-directory-above-a-scratch-root-is-not-a-worktree")
def test_attesting_a_scratch_root_does_not_create_git_markers_on_its_ancestors(
    tmp_path: Path,
) -> None:
    """The parent walk reads `.git`; it never creates the marker it is looking for."""

    scratch = agent_scratch_root(tmp_path / "nested")
    before = tuple(
        (
            ancestor,
            (ancestor / ".git").exists(),
            (ancestor / ".git").is_symlink(),
        )
        for ancestor in (scratch, *scratch.parents)
    )

    LocalAgentAttemptWorkspaceOwner(scratch).close()

    for ancestor, existed, linked in before:
        marker = ancestor / ".git"
        assert marker.exists() is existed
        assert marker.is_symlink() is linked


@pytest.mark.proves("an-unusable-scratch-root-is-refused-before-the-server-exists")
def test_a_scratch_root_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root this server cannot own is refused rather than written into."""

    scratch_root = agent_scratch_root(tmp_path)
    somebody_else = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: somebody_else)

    with pytest.raises(AgentScratchRootRefused, match="another user"):
        LocalAgentAttemptWorkspaceOwner(scratch_root)


@pytest.mark.proves("an-unusable-scratch-root-is-refused-before-the-server-exists")
def test_a_refused_scratch_root_leaves_no_descriptor_behind(tmp_path: Path) -> None:
    """A server that keeps serving after a refusal keeps no handle on that root."""

    scratch_root = agent_scratch_root(tmp_path)
    (scratch_root / "left-behind").mkdir()
    before = open_descriptors()

    for _refusal in range(3):
        with pytest.raises(AgentScratchRootRefused, match="no attempt workspace"):
            LocalAgentAttemptWorkspaceOwner(scratch_root)

    assert open_descriptors() == before


@pytest.mark.proves("an-unusable-scratch-root-is-refused-before-the-server-exists")
def test_a_root_path_replaced_after_binding_refuses_instead_of_leasing(
    tmp_path: Path,
) -> None:
    """The bound directory, not the name, is what an attempt is handed."""

    owner = agent_workspace_owner(tmp_path)
    bound_root = owner.scratch_root
    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId.for_node(
            RunId("lease/replaced"), WorkflowRevisionHash("4" * 64), "build"
        ),
        AgentExecutionRequestHash("5" * 64),
    )
    impostor = tmp_path / "impostor"
    impostor.mkdir(mode=SCRATCH_ROOT_MODE)
    bound_root.rename(tmp_path / "moved-away")
    impostor.rename(bound_root)

    with pytest.raises(AgentScratchRootRefused, match="no longer names"):
        owner.acquire(attempt_id)

    assert workspace_names(bound_root) == set()
    assert workspace_names(tmp_path / "moved-away") == set()


@pytest.mark.proves(
    "an-occupied-attempt-path-starts-no-provider-and-survives-everything-after"
)
def test_a_preexisting_attempt_path_refuses_the_attempt_and_starts_no_provider(
    tmp_path: Path,
) -> None:
    """Sensitive files already standing where an attempt would run are untouched."""

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "lease/occupied"))
        owner = runtime_workspace_owner(runtime)
        occupied = owner.scratch_root / execution.attempt_id.value
        occupied.mkdir(mode=SCRATCH_ROOT_MODE)
        for name in OBSERVED_PROVIDER_FILES:
            (occupied / name).write_text(f"the operator's {name}", encoding="utf-8")
        before = snapshot(occupied)
        executor = MaterializingExecutor(sentinel_directory(tmp_path))
        store = DbosAgentAttemptStore(runtime.engine)

        with pytest.raises(AgentAttemptWorkspaceRefused):
            execute_agent_attempt(
                execution,
                executor,
                store,
                runtime.agent_process_supervisor,
                owner,
                permissions=GRANTS_NOTHING,
                workspace_files=workspace_files_nobody_opens,
            )

        assert snapshot(occupied) == before
        assert executor.decodes == 0
        assert store.load(execution.attempt_id).state is AgentAttemptState.LAUNCH_ARMED
    finally:
        runtime.close()


@pytest.mark.proves(
    "an-occupied-attempt-path-starts-no-provider-and-survives-everything-after"
)
def test_a_refused_attempt_path_survives_the_cancellation_of_its_attempt(
    tmp_path: Path,
) -> None:
    """Ending an attempt removes the directory it leased, never one it was refused."""

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "lease/occupied-cancel")
        )
        owner = runtime_workspace_owner(runtime)
        occupied = owner.scratch_root / execution.attempt_id.value
        occupied.mkdir(mode=SCRATCH_ROOT_MODE)
        (occupied / ".env").write_text("the operator's own secret", encoding="utf-8")
        before = snapshot(occupied)
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        with pytest.raises(AgentAttemptWorkspaceRefused):
            execute_agent_attempt(
                execution,
                MaterializingExecutor(sentinel_directory(tmp_path)),
                store,
                runtime.agent_process_supervisor,
                owner,
                permissions=GRANTS_NOTHING,
                workspace_files=workspace_files_nobody_opens,
            )
        armed = store.load(execution.attempt_id)
        request = CancelAgentAttemptRequest(
            armed.run_id,
            armed.attempt_id,
            "cancel-occupied",
            armed.state_version,
            AgentAttemptReplacement.NONE,
        )
        store.request_cancellation(request)

        accepted = continue_agent_attempt_cancellation(
            request, store, runtime.agent_process_supervisor, owner
        )

        assert accepted is not None
        assert accepted.attempt.state is AgentAttemptState.CANCELLED
        assert snapshot(occupied) == before
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("return_code", "terminal"),
    [
        pytest.param(0, AgentAttemptState.SUCCEEDED, id="success"),
        pytest.param(7, AgentAttemptState.FAILED, id="known failure"),
    ],
)
@pytest.mark.proves(
    "a-workspace-falls-only-behind-the-two-facts-that-make-removal-safe"
)
def test_a_terminal_attempt_leaves_its_workspace_and_nothing_else_removed(
    tmp_path: Path, return_code: int, terminal: AgentAttemptState
) -> None:
    """The provider writes what it likes inside; only that directory goes."""

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, f"lease/terminal/{return_code}")
        )
        owner = runtime_workspace_owner(runtime)
        sentinel = sentinel_directory(tmp_path)
        store = DbosAgentAttemptStore(runtime.engine)
        executor = MaterializingExecutor(sentinel, return_code)

        execute_agent_attempt(
            execution,
            executor,
            store,
            runtime.agent_process_supervisor,
            owner,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

        assert store.load(execution.attempt_id).state is terminal
        assert executor.decodes == 1
        assert workspace_names(owner.scratch_root) == set()
        assert owner.scratch_root.is_dir()
        assert sentinel_survived(sentinel)
    finally:
        runtime.close()


@pytest.mark.proves("every-attempt-runs-in-a-blank-directory-of-its-own")
def test_a_provider_really_materializes_the_observed_write_set(
    tmp_path: Path,
) -> None:
    """The cleanup proof is worth having only if something was there to remove."""

    owner = agent_workspace_owner(tmp_path)
    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId.for_node(
            RunId("lease/write-set"), WorkflowRevisionHash("4" * 64), "build"
        ),
        AgentExecutionRequestHash("5" * 64),
    )
    sentinel = sentinel_directory(tmp_path)
    lease = owner.acquire(attempt_id)

    completed = subprocess.run(
        materializing_command(sentinel).arguments,
        cwd=lease.working_directory,
        check=True,
    )

    assert completed.returncode == 0
    workspace = lease.working_directory
    assert {
        name for name in OBSERVED_PROVIDER_FILES if (workspace / name).is_file()
    } == set(OBSERVED_PROVIDER_FILES)
    assert {
        name for name in OBSERVED_PROVIDER_DIRECTORIES if (workspace / name).is_dir()
    } == set(OBSERVED_PROVIDER_DIRECTORIES)
    assert (workspace / PROVIDER_LINK_NAME).is_symlink()

    owner.release(attempt_id)

    assert not workspace.exists()
    assert sentinel_survived(sentinel)


@pytest.mark.proves(
    "a-workspace-falls-only-behind-the-two-facts-that-make-removal-safe"
)
def test_a_workspace_nested_deeper_than_recursion_allows_is_still_removed(
    tmp_path: Path,
) -> None:
    """No tree a provider may legally build can leave a workspace nobody removes."""

    owner = agent_workspace_owner(tmp_path)
    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId.for_node(
            RunId("lease/deep"), WorkflowRevisionHash("4" * 64), "build"
        ),
        AgentExecutionRequestHash("5" * 64),
    )
    sentinel = sentinel_directory(tmp_path)
    workspace = owner.acquire(attempt_id).working_directory
    deepest = workspace
    for _level in range(PROVIDER_TREE_DEPTH_BEYOND_RECURSION):
        deepest = deepest / "d"
        deepest.mkdir()
    (deepest / ".env").write_bytes(b"")
    (workspace / PROVIDER_LINK_NAME).symlink_to(sentinel)

    owner.release(attempt_id)

    assert not workspace.exists()
    assert workspace_names(owner.scratch_root) == set()
    assert sentinel_survived(sentinel)


@pytest.mark.proves(
    "a-workspace-falls-only-behind-the-two-facts-that-make-removal-safe"
)
def test_a_cancelled_attempt_loses_its_workspace_only_behind_attested_cleanup(
    tmp_path: Path,
) -> None:
    """The directory outlives the provider until the cleanup is durably recorded."""

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "lease/cancel"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        workspaces = WatchingWorkspaceOwner(
            runtime_workspace_owner(runtime), store, watched=execution.attempt_id
        )
        ready = tmp_path / "provider-ready"
        sentinel = sentinel_directory(tmp_path)
        failures: list[RuntimeError] = []

        def run_attempt() -> None:
            try:
                execute_agent_attempt(
                    execution,
                    WaitingExecutor(ready),
                    store,
                    runtime.agent_process_supervisor,
                    workspaces,
                    permissions=GRANTS_NOTHING,
                    workspace_files=workspace_files_nobody_opens,
                )
            except RuntimeError as error:
                failures.append(error)

        worker = threading.Thread(target=run_attempt)
        worker.start()
        _wait_until(ready.exists)
        # The ready file is not the durable launch. `observe_process` still
        # bumps `state_version` after the child has started; a cancel loaded
        # before that write is stale, never records the command, and the
        # attestation then names a missing cancellation.
        _wait_until(
            lambda: (
                store.load(execution.attempt_id).process_phase
                is AgentAttemptProcessPhase.PROCESS_OBSERVED
            )
        )
        workspace = workspaces.owner.scratch_root / execution.attempt_id.value
        assert (workspace / ".env").is_file()

        current = store.load(execution.attempt_id)
        request = CancelAgentAttemptRequest(
            current.run_id,
            current.attempt_id,
            "cancel-lease",
            current.state_version,
            AgentAttemptReplacement.NONE,
        )
        assert isinstance(
            store.request_cancellation(request), AgentAttemptCancellationAccepted
        )
        accepted = continue_agent_attempt_cancellation(
            request, store, runtime.agent_process_supervisor, workspaces
        )
        worker.join(timeout=10)

        assert accepted is not None
        assert accepted.attempt.state is AgentAttemptState.CANCELLED
        assert workspaces.observed[-1] == ("release", AgentAttemptState.CANCELLED)
        assert not workspace.exists()
        assert workspaces.owner.scratch_root.is_dir()
        assert sentinel_survived(sentinel)
        assert len(failures) == 1
    finally:
        runtime.close()


def _wait_until(condition: Callable[[], bool], timeout_seconds: float = 15) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("the awaited condition never held")


def durable_attempt(state: AgentAttemptState) -> AgentAttempt:
    """One durable attempt in the exact shape its state requires."""

    run_id = RunId("reconcile/run")
    revision_hash = WorkflowRevisionHash("4" * 64)
    node_id = state.value.lower()
    node_execution_id = NodeExecutionId.for_node(run_id, revision_hash, node_id)
    request_hash = AgentExecutionRequestHash("5" * 64)
    cancelled = AgentAttemptCancellation(
        "reconcile-command",
        1,
        AgentAttemptReplacement.NONE,
        AgentAttemptRedriveState.CLEANUP_ATTESTED,
        AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
    )
    shapes: dict[AgentAttemptState, dict[str, object]] = {
        AgentAttemptState.PREPARED: {"state_version": 0},
        AgentAttemptState.LAUNCH_ARMED: {"state_version": 1},
        AgentAttemptState.CANCEL_REQUESTED: {
            "state_version": 1,
            "cancellation": AgentAttemptCancellation(
                "reconcile-command", 1, AgentAttemptReplacement.NONE
            ),
        },
        AgentAttemptState.SUCCEEDED: {
            "state_version": 2,
            "receipt_hash": AgentReceiptHash("6" * 64),
        },
        AgentAttemptState.FAILED: {
            "state_version": 2,
            "failure_code": AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        },
        AgentAttemptState.CANCELLED: {
            "state_version": 2,
            "cancellation": cancelled,
            "process_phase": AgentAttemptProcessPhase.CLEANUP_ATTESTED,
        },
        AgentAttemptState.INTERRUPTED: {
            "state_version": 2,
            "cancellation": cancelled,
            "process_phase": AgentAttemptProcessPhase.CLEANUP_ATTESTED,
        },
    }
    return AgentAttempt(
        AgentAttemptId.for_execution(node_execution_id, request_hash),
        node_execution_id,
        request_hash,
        AgentExecutorOperationalIdentity("reconcile-test"),
        run_id,
        revision_hash,
        node_id,
        1,
        state,
        **shapes[state],  # pyright: ignore[reportArgumentType]
    )


@dataclass
class RecordedAttempts:
    """Exactly the durable attempts a restart can read back."""

    attempts: tuple[AgentAttempt, ...]

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt:
        for attempt in self.attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
        raise LookupError(f"no durable attempt {attempt_id.value}")


@pytest.mark.parametrize(
    "state",
    [
        AgentAttemptState.SUCCEEDED,
        AgentAttemptState.FAILED,
        AgentAttemptState.CANCELLED,
        AgentAttemptState.INTERRUPTED,
    ],
)
@pytest.mark.proves("a-restart-removes-what-terminal-attempts-left-and-nothing-else")
def test_restart_removes_the_workspace_of_every_terminal_attempt(
    tmp_path: Path, state: AgentAttemptState
) -> None:
    owner = agent_workspace_owner(tmp_path)
    attempt = durable_attempt(state)
    workspace = owner.acquire(attempt.attempt_id).working_directory
    (workspace / ".env").write_bytes(b"")

    owner.reconcile(RecordedAttempts((attempt,)))
    owner.reconcile(RecordedAttempts((attempt,)))

    assert not workspace.exists()
    assert owner.scratch_root.is_dir()


@pytest.mark.parametrize(
    "state",
    [
        AgentAttemptState.PREPARED,
        AgentAttemptState.LAUNCH_ARMED,
        AgentAttemptState.CANCEL_REQUESTED,
    ],
)
@pytest.mark.proves("a-restart-removes-what-terminal-attempts-left-and-nothing-else")
def test_restart_preserves_the_workspace_of_every_nonterminal_attempt(
    tmp_path: Path, state: AgentAttemptState
) -> None:
    owner = agent_workspace_owner(tmp_path)
    attempt = durable_attempt(state)
    workspace = owner.acquire(attempt.attempt_id).working_directory
    (workspace / ".env").write_text("half-written", encoding="utf-8")
    before = snapshot(workspace)

    owner.reconcile(RecordedAttempts((attempt,)))

    assert snapshot(workspace) == before


@pytest.mark.proves(
    "an-occupied-attempt-path-starts-no-provider-and-survives-everything-after"
)
def test_a_refused_attempt_path_survives_the_restart_that_follows(
    tmp_path: Path,
) -> None:
    """Reconciliation removes what this root leased, not what merely bears a name."""

    owner = agent_workspace_owner(tmp_path)
    attempt = durable_attempt(AgentAttemptState.CANCELLED)
    occupied = owner.scratch_root / attempt.attempt_id.value
    occupied.mkdir(mode=SCRATCH_ROOT_MODE)
    (occupied / ".env").write_text("the operator's own secret", encoding="utf-8")
    before = snapshot(occupied)

    owner.reconcile(RecordedAttempts((attempt,)))
    owner.reconcile(RecordedAttempts((attempt,)))

    assert snapshot(occupied) == before


@pytest.mark.proves("a-restart-removes-what-terminal-attempts-left-and-nothing-else")
def test_restart_refuses_a_workspace_no_durable_attempt_owns(tmp_path: Path) -> None:
    owner = agent_workspace_owner(tmp_path)
    attempt = durable_attempt(AgentAttemptState.SUCCEEDED)
    workspace = owner.acquire(attempt.attempt_id).working_directory

    with pytest.raises(LookupError):
        owner.reconcile(RecordedAttempts(()))

    assert workspace.is_dir()


@pytest.mark.proves("a-restart-removes-what-terminal-attempts-left-and-nothing-else")
def test_restart_refuses_a_name_that_is_no_attempt_workspace(tmp_path: Path) -> None:
    owner = agent_workspace_owner(tmp_path)
    attempt = durable_attempt(AgentAttemptState.SUCCEEDED)
    workspace = owner.acquire(attempt.attempt_id).working_directory
    (owner.scratch_root / "left-behind").mkdir()

    with pytest.raises(AgentScratchRootRefused, match="no attempt workspace"):
        owner.reconcile(RecordedAttempts((attempt,)))

    assert workspace.is_dir()


@pytest.mark.proves(
    "a-workspace-falls-only-behind-the-two-facts-that-make-removal-safe"
)
def test_a_cleanup_that_cannot_finish_stays_visible_and_removes_nothing_wider(
    tmp_path: Path,
) -> None:
    owner = agent_workspace_owner(tmp_path)
    terminal = durable_attempt(AgentAttemptState.SUCCEEDED)
    live = durable_attempt(AgentAttemptState.LAUNCH_ARMED)
    unremovable = owner.acquire(terminal.attempt_id).working_directory
    (unremovable / ".env").write_bytes(b"")
    preserved = owner.acquire(live.attempt_id).working_directory
    unremovable.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            owner.reconcile(RecordedAttempts((terminal, live)))
    finally:
        unremovable.chmod(SCRATCH_ROOT_MODE)

    assert (unremovable / ".env").is_file()
    assert preserved.is_dir()


@pytest.mark.proves("no-workspace-path-or-content-becomes-durable-state")
def test_no_workspace_path_or_content_reaches_any_durable_row_or_event(
    tmp_path: Path,
) -> None:
    """The scratch root is an operational detail; the product never records it."""

    canary = "scratch-canary-9d41f7"
    runtime = attempt_runtime(tmp_path / canary)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "lease/leak"))
        owner = runtime_workspace_owner(runtime)

        execute_agent_attempt(
            execution,
            MaterializingExecutor(sentinel_directory(tmp_path)),
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            owner,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

        recorded = "\n".join(_product_rows(runtime.engine))
        assert canary not in recorded
        assert str(owner.scratch_root) not in recorded
        assert execution.attempt_id.value in recorded
        for name in OBSERVED_PROVIDER_FILES:
            assert name not in recorded
    finally:
        runtime.close()


def _product_rows(engine: sa.Engine) -> Iterator[str]:
    with engine.connect() as connection:
        for table_name in sorted(PRODUCT_TABLE_NAMES):
            table = metadata.tables[table_name]
            for row in connection.execute(sa.select(table)).mappings():
                yield repr(dict(row))


@dataclass
class ReportingExecutor(PrintModeExecutor):
    """One real executor, keeping whatever answer it decoded."""

    inner: AgentExecutorV2
    reported: list[bytes] = field(default_factory=list)

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        return self.inner.prepare_process(request)

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        result = self.inner.decode_process_completion(invocation, completion)
        if isinstance(result, AgentExecutionResult):
            self.reported.append(result.output_bytes)
        return result

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        self.inner.close()


@dataclass
class DirectoryReportingExecutor(PrintModeExecutor):
    """A provider of no particular vendor, answering with where it was started."""

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(
            (
                sys.executable,
                "-c",
                "import json, os; print(json.dumps(os.getcwd()), end='')",
            ),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        return AgentExecutionResult(completion.standard_output)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return


def claude_subscription_conformance(
    tmp_path: Path,
) -> tuple[DbosRuntime, AgentAttemptExecution, AgentExecutorV2]:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(deployment, INTROSPECTING_CLAUDE)
    runtime = claude_subscription_runtime(tmp_path, settings)
    runtime.initialize_storage()
    return (
        runtime,
        claude_subscription_attempt(runtime, "conformance/claude"),
        ClaudeSubscriptionExecutorFactory(settings).open(),
    )


def provider_neutral_conformance(
    tmp_path: Path,
) -> tuple[DbosRuntime, AgentAttemptExecution, AgentExecutorV2]:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    return (
        runtime,
        agent_attempt_execution(attempt_request(runtime, "conformance/neutral")),
        DirectoryReportingExecutor(),
    )


@pytest.mark.parametrize(
    "conformance",
    [claude_subscription_conformance, provider_neutral_conformance],
    ids=["claude subscription", "another provider"],
)
@pytest.mark.proves("every-attempt-runs-in-a-blank-directory-of-its-own")
def test_every_provider_runs_in_the_workspace_its_own_attempt_leased(
    tmp_path: Path,
    conformance: Callable[
        [Path], tuple[DbosRuntime, AgentAttemptExecution, AgentExecutorV2]
    ],
) -> None:
    """One workspace owner serves every provider, and none of them names it."""

    runtime, execution, inner = conformance(tmp_path)
    try:
        owner = runtime_workspace_owner(runtime)
        executor = ReportingExecutor(inner)
        expected = owner.scratch_root / execution.attempt_id.value

        outcome = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            owner,
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

        assert isinstance(outcome, AgentAttemptSucceeded)
        assert str(expected).encode("utf-8") in executor.reported[0]
        assert not expected.exists()
        assert workspace_names(owner.scratch_root) == set()
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "module",
    [
        "src/atelier2/ports/agent_executions.py",
        "src/atelier2/adapters/agent_workspaces.py",
        "src/atelier2/application/execute_agent_attempt.py",
        "src/atelier2/application/cancel_agent_attempt.py",
    ],
)
@pytest.mark.proves("the-lease-and-its-lifecycle-name-no-provider")
def test_the_workspace_lease_and_its_lifecycle_name_no_provider(module: str) -> None:
    """A lease that named one provider would be that provider's, not an attempt's."""

    source = (Path(__file__).parents[2] / module).read_text(encoding="utf-8").lower()

    assert "claude" not in source
    assert "anthropic" not in source


@pytest.mark.proves("a-restart-removes-what-terminal-attempts-left-and-nothing-else")
def test_binding_the_durable_database_again_reconciles_what_a_crash_left(
    tmp_path: Path,
) -> None:
    """The restart, not a caller, is what clears an abandoned workspace."""

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    scratch_root = runtime_workspace_owner(runtime).scratch_root
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "lease/restart"))
        execute_agent_attempt(
            execution,
            MaterializingExecutor(sentinel_directory(tmp_path)),
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )
    finally:
        runtime.close()
    # What a process killed between its provider's exit and its own cleanup
    # would have left behind: a terminal attempt with the workspace it leased,
    # so the lease is taken through the same owner a serving process uses.
    killed = LocalAgentAttemptWorkspaceOwner(scratch_root)
    abandoned = killed.acquire(execution.attempt_id).working_directory
    (abandoned / ".env").write_bytes(b"")
    killed.close()

    restarted = attempt_runtime(tmp_path)

    try:
        assert not abandoned.exists()
        assert workspace_names(scratch_root) == set()
    finally:
        restarted.close()


@pytest.mark.proves("only-a-directory-this-owner-created-is-ever-removed")
def test_a_lease_mark_this_owner_never_wrote_removes_nothing_on_cancellation(
    tmp_path: Path,
) -> None:
    """A mark is provenance only if this owner wrote it while creating the ground.

    An attempt path the operator's own files already stand in is refused. If
    somebody can also place the mark that says "this directory is mine to
    remove", the refusal buys nothing: the cancellation that follows deletes the
    operator's directory on the strength of a file it never wrote.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "lease/forged-mark-cancel")
        )
        owner = runtime_workspace_owner(runtime)
        occupied = owner.scratch_root / execution.attempt_id.value
        occupied.mkdir(mode=SCRATCH_ROOT_MODE)
        (occupied / ".env").write_text("the operator's own secret", encoding="utf-8")
        forged = owner.scratch_root / f"{execution.attempt_id.value}.lease"
        forged.write_bytes(b"")
        before = snapshot(occupied)
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        with pytest.raises(AgentAttemptWorkspaceRefused):
            execute_agent_attempt(
                execution,
                MaterializingExecutor(sentinel_directory(tmp_path)),
                store,
                runtime.agent_process_supervisor,
                owner,
                permissions=GRANTS_NOTHING,
                workspace_files=workspace_files_nobody_opens,
            )
        armed = store.load(execution.attempt_id)
        request = CancelAgentAttemptRequest(
            armed.run_id,
            armed.attempt_id,
            "cancel-forged",
            armed.state_version,
            AgentAttemptReplacement.NONE,
        )
        store.request_cancellation(request)

        # A mark nobody here wrote is not a reason to shrug: it means somebody
        # wrote into this server's own scratch root. The removal is refused and
        # the refusal is loud, while the operator's files stand untouched.
        with pytest.raises(AgentAttemptWorkspaceRefused):
            continue_agent_attempt_cancellation(
                request, store, runtime.agent_process_supervisor, owner
            )

        assert snapshot(occupied) == before
    finally:
        runtime.close()


@pytest.mark.proves("only-a-directory-this-owner-created-is-ever-removed")
def test_a_directory_replaced_under_its_lease_is_not_removed_by_the_restart(
    tmp_path: Path,
) -> None:
    """The mark names one directory, not one path.

    A workspace this owner leased and then lost -- the directory removed and a
    different one moved into its place while the process was down -- is not the
    directory the mark stands for. Reconciliation removes what it leased, and
    an inode it never created is somebody else's.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        owner = runtime_workspace_owner(runtime)
        attempt_id = AgentAttemptId.of(b"lease/replaced-under-its-mark")
        lease = owner.acquire(attempt_id)
        impostor = tmp_path / "impostor"
        impostor.mkdir(mode=SCRATCH_ROOT_MODE)
        (impostor / ".env").write_text("the operator's own secret", encoding="utf-8")
        shutil.rmtree(lease.working_directory)
        impostor.rename(lease.working_directory)
        before = snapshot(lease.working_directory)

        with pytest.raises(AgentAttemptWorkspaceRefused):
            owner.release(attempt_id)

        assert snapshot(lease.working_directory) == before
    finally:
        runtime.close()


@pytest.mark.proves("only-a-directory-this-owner-created-is-ever-removed")
def test_a_lease_mark_standing_without_its_directory_refuses_the_next_acquire(
    tmp_path: Path,
) -> None:
    """A mark this owner did not just write is never adopted as provenance.

    The state is reachable without an impostor: `release` removes the tree and
    unlinks the mark, so a crash between the two leaves a mark with no
    directory. Adopting it would let the next lease inherit a provenance it did
    not create, which is the whole weight the later removal rests on. It is
    refused by name instead, and nothing is created.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        owner = runtime_workspace_owner(runtime)
        attempt_id = AgentAttemptId.of(b"lease/mark-without-its-directory")
        stale = owner.scratch_root / f"{attempt_id.value}.lease"
        stale.write_bytes(b"")

        with pytest.raises(AgentAttemptWorkspaceRefused, match="already stands"):
            owner.acquire(attempt_id)

        assert not (owner.scratch_root / attempt_id.value).exists()
        assert stale.read_bytes() == b""
    finally:
        runtime.close()


@pytest.mark.proves("every-attempt-runs-in-a-blank-directory-of-its-own")
def test_a_directory_swapped_after_the_lease_never_becomes_the_provider_ground(
    tmp_path: Path,
) -> None:
    """The lease attests a directory; the launch must enter that one, not that path.

    Between `acquire` and the launch there is a window. A peer of the same user
    who replaces the leased directory in that window hands the provider somebody
    else's ground -- with the operator's own files in it -- while every record
    still says the attempt ran where it leased. The lease is an identity, and the
    launch has to enter the identity rather than resolve the name again.
    """

    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "lease/swapped-before-launch")
        )
        owner = runtime_workspace_owner(runtime)
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        store.prepare(execution)
        lease = owner.acquire(execution.attempt_id)
        leased = os.stat(lease.working_directory, follow_symlinks=False)

        impostor = tmp_path / "impostor"
        impostor.mkdir(mode=SCRATCH_ROOT_MODE)
        (impostor / ".env").write_text("the operator's own secret", encoding="utf-8")
        shutil.rmtree(lease.working_directory)
        impostor.rename(lease.working_directory)
        standing = os.stat(lease.working_directory, follow_symlinks=False)
        assert (standing.st_dev, standing.st_ino) != (leased.st_dev, leased.st_ino)

        executor = DirectoryReportingExecutor()
        command = executor.prepare_process(execution.request)
        runtime.agent_process_supervisor.prepare(execution)
        assert isinstance(store.claim(execution), AgentAttemptClaimedByThisCall)

        # The launch is refused before a process exists. It surfaces as the
        # landed refusal for every malformed launch -- this call owns no
        # process -- and what matters is that no provider ever started.
        with pytest.raises(AgentProcessOwnerNotLocal):
            runtime.agent_process_supervisor.launch_and_wait(
                execution,
                AgentProcessInvocation(command, lease),
                NOTHING_IS_PERMITTED,
            )

        # The impostor is untouched: no provider was ever started in it.
        standing_marker = lease.working_directory / ".env"
        assert standing_marker.read_text(encoding="utf-8") == (
            "the operator's own secret"
        )
    finally:
        runtime.close()


def test_the_launch_names_the_identity_that_changed_under_the_lease(
    tmp_path: Path,
) -> None:
    """The refusal says which thing changed, not merely that something did."""

    leased = tmp_path / "leased"
    leased.mkdir(mode=SCRATCH_ROOT_MODE)
    standing = os.stat(leased, follow_symlinks=False)

    with entered_leased_directory(leased, standing.st_dev, standing.st_ino) as (
        entry,
        descriptor,
    ):
        assert entry == f"/proc/self/fd/{descriptor}"
        assert os.stat(entry).st_ino == standing.st_ino

    with (
        pytest.raises(LeasedDirectoryChanged, match="identity changed"),
        entered_leased_directory(leased, standing.st_dev, standing.st_ino + 1),
    ):
        pass
