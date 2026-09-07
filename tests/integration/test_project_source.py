"""The project an attempt works in is one commit's tree, unpacked where it runs.

Two facts decide everything here. What is read is what the pinned commit carries,
never what the operator's checkout holds now -- so a commit landing while a run is
in flight cannot change what that run works on. And what an attempt is given is a
linked worktree of the source at that commit, in the directory the attempt leased
and entered through the identity that lease attested, detachable into material:
the tree without its repository.

A pin the source can no longer answer for is a refusal in the source's own words,
never a run that quietly works on nothing.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.node_binding_codec import (
    decode_node_binding,
    encode_node_binding,
)
from atelier2.adapters.leased_directory import LeasedDirectoryChanged
from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.adapters.project_verification import declared_project
from atelier2.application.bind_node import pinned_project
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import AgentExecutionRequestV2, AgentExecutionResult
from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.node_bindings import AgentNodeBindingV2
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
    PrintModeExecutor,
)
from atelier2.ports.project_source import ProjectSourceUnavailable
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
    leased_directory_identity,
    resolved_agent_binding,
    runtime_workspace_owner,
    workspace_files_nobody_opens,
)
from tests.scenarios.projects import (
    commit_to_project,
    declared_in_checkout,
    git_project,
    run_git,
    write_into_checkout,
)

MANIFEST = PurePosixPath("pyproject.toml")
COMMITTED = "[project]\nname = 'as it was committed'\n"
EDITED_AFTERWARDS = "[project]\nname = 'only in the checkout'\n"
LANE = HeadBranch("atelier2/work-item/lease")


def lease(tmp_path: Path, name: str = "lease") -> AgentAttemptWorkspaceLease:
    return leased_directory_identity(AgentAttemptId("a1" * 32), tmp_path / name)


def worktree_facts(working_directory: Path) -> dict[str, str]:
    """What git says a checkout is: where its repository is, what it stands on."""

    return {
        "git_dir": run_git(working_directory, "rev-parse", "--git-dir"),
        "common_dir": run_git(working_directory, "rev-parse", "--git-common-dir"),
        "branch": run_git(working_directory, "branch", "--show-current"),
        "head": run_git(working_directory, "rev-parse", "HEAD"),
        "status": run_git(working_directory, "status", "--porcelain"),
        "files": run_git(working_directory, "ls-files"),
    }


def registered_worktrees(root: Path) -> set[str]:
    listed = run_git(root, "worktree", "list", "--porcelain").splitlines()
    return {
        line.removeprefix("worktree ")
        for line in listed
        if line.startswith("worktree ")
    }


def tree_of(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.mark.proves("what-a-project-declares-and-where-it-runs-are-one-commit")
def test_the_pinned_tree_is_read_as_committed_however_the_checkout_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, {MANIFEST.name: COMMITTED})
    source = LocalGitProjectSource(root)

    write_into_checkout(root, {MANIFEST.name: EDITED_AFTERWARDS})

    assert source.read(pin, MANIFEST).decode("utf-8") == COMMITTED


@pytest.mark.proves("what-a-project-declares-and-where-it-runs-are-one-commit")
def test_a_later_commit_moves_the_head_and_leaves_the_earlier_pin_readable(
    tmp_path: Path,
) -> None:
    """Pinning at one moment is what makes a started run repeatable at all."""

    root = tmp_path / "project"
    first = git_project(root, {MANIFEST.name: COMMITTED})
    second = commit_to_project(root, {MANIFEST.name: EDITED_AFTERWARDS})
    source = LocalGitProjectSource(root)

    assert (first.commit, first.tree) != (second.commit, second.tree)
    assert source.head() == second
    assert source.read(first, MANIFEST).decode("utf-8") == COMMITTED
    assert source.read(second, MANIFEST).decode("utf-8") == EDITED_AFTERWARDS


@pytest.mark.parametrize(
    ("branch", "shown"),
    [(LANE, LANE.value), (None, "")],
    ids=["on the lane branch", "detached at the pin"],
)
@pytest.mark.proves("an-attempt-works-in-the-tree-its-own-binding-pinned")
def test_the_pinned_commit_is_checked_out_into_the_lease_as_a_linked_worktree(
    tmp_path: Path, branch: HeadBranch | None, shown: str
) -> None:
    root = tmp_path / "project"
    pin = git_project(
        root, {MANIFEST.name: COMMITTED, "src/tool.py": "print('committed')\n"}
    )
    write_into_checkout(root, {"src/tool.py": "print('only in the checkout')\n"})
    leased = lease(tmp_path)

    LocalGitProjectSource(root).materialize(pin, leased, branch)

    checked_out = leased.working_directory
    assert (checked_out / MANIFEST.name).read_text(encoding="utf-8") == COMMITTED
    assert (checked_out / "src/tool.py").read_text(
        encoding="utf-8"
    ) == "print('committed')\n"
    facts = worktree_facts(checked_out)
    assert facts["git_dir"] != facts["common_dir"]
    assert (facts["branch"], facts["head"], facts["status"]) == (shown, pin.commit, "")
    assert facts["files"] != ""


@pytest.mark.proves("an-attempt-works-in-the-tree-its-own-binding-pinned")
def test_detaching_the_lease_removes_the_worktree_pointer_and_keeps_the_tree(
    tmp_path: Path,
) -> None:
    """Detached, the lease is material: what stands there stays, minus `.git`."""

    root = tmp_path / "project"
    pin = git_project(root, {MANIFEST.name: COMMITTED})
    leased = lease(tmp_path)
    source = LocalGitProjectSource(root)
    source.materialize(pin, leased, LANE)
    write_into_checkout(leased.working_directory, {"made.py": "by the attempt\n"})
    standing = tree_of(leased.working_directory)
    assert ".git" in standing

    source.detach_from_repository(leased)
    source.detach_from_repository(leased)

    assert tree_of(leased.working_directory) == {
        name: body for name, body in standing.items() if name != ".git"
    }
    assert registered_worktrees(root) == {str(root)}
    assert run_git(root, "rev-parse", LANE.full_ref) == pin.commit


@pytest.mark.proves("an-attempt-works-in-the-tree-its-own-binding-pinned")
def test_a_lane_branch_a_standing_worktree_holds_refuses_the_next_lease(
    tmp_path: Path,
) -> None:
    """Two attempts never share a branch; a detached lease frees it at the pin."""

    root = tmp_path / "project"
    pin = git_project(root, {MANIFEST.name: COMMITTED})
    source = LocalGitProjectSource(root)
    first, second = lease(tmp_path, "first"), lease(tmp_path, "second")
    source.materialize(pin, first, LANE)

    with pytest.raises(ProjectSourceUnavailable, match=LANE.value):
        source.materialize(pin, second, LANE)
    assert list(second.working_directory.iterdir()) == []

    source.detach_from_repository(first)
    source.materialize(pin, second, LANE)

    assert worktree_facts(second.working_directory)["head"] == pin.commit


POISONED_SMUDGE = "sed s/./X/g"
"""A filter driver rewriting every byte it is handed, as git-lfs and its kind do."""


@pytest.mark.proves("an-attempt-works-in-the-tree-its-own-binding-pinned")
def test_a_filter_the_checkout_declares_refuses_the_lease_by_name(
    tmp_path: Path,
) -> None:
    """No lease is checked out under a driver that could rewrite the pinned tree.

    A checkout's own `.git/config` can declare a `filter` driver that its
    `.gitattributes` points paths at. What comes back out of a lease is read
    under no filter at all, so a smudge here would hand the attempt content the
    pin does not carry and have it come home as work the attempt never did.
    """

    root = tmp_path / "project"
    pin = git_project(
        root, {".gitattributes": "* filter=poison\n", "src/tool.py": COMMITTED}
    )
    declared_in_checkout(
        root, {"filter.poison.smudge": POISONED_SMUDGE, "filter.poison.clean": "cat"}
    )
    leased = lease(tmp_path)

    with pytest.raises(ProjectSourceUnavailable, match="filter.poison.smudge"):
        LocalGitProjectSource(root).materialize(pin, leased, LANE)

    assert list(leased.working_directory.iterdir()) == []
    assert run_git(root, "branch", "--list", LANE.value) == ""


@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_commit_this_source_cannot_answer_for_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """The refusal names the pin, because a run on the wrong tree is the harm."""

    root = tmp_path / "project"
    git_project(root, {MANIFEST.name: COMMITTED})
    lost = ProjectSourcePin("f0" * 20, "e1" * 20)

    with pytest.raises(ProjectSourceUnavailable, match=lost.commit):
        LocalGitProjectSource(root).attest(lost)


@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_commit_this_source_cannot_answer_for_unpacks_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    git_project(root, {MANIFEST.name: COMMITTED})
    leased = lease(tmp_path)

    with pytest.raises(ProjectSourceUnavailable):
        LocalGitProjectSource(root).materialize(
            ProjectSourcePin("f0" * 20, "e1" * 20), leased
        )

    assert list(leased.working_directory.iterdir()) == []


def test_the_pin_a_source_answers_with_is_the_one_it_attests(tmp_path: Path) -> None:
    root = tmp_path / "project"
    pin = git_project(root, {MANIFEST.name: COMMITTED})

    LocalGitProjectSource(root).attest(pin)


def test_a_root_that_is_no_repository_is_refused_by_name(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repository"
    plain.mkdir()

    with pytest.raises(ProjectSourceUnavailable, match=str(plain)):
        LocalGitProjectSource(plain).head()


def test_a_root_below_its_repository_top_level_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """Pinning there would carry the enclosing repository, and read its manifest."""

    root = tmp_path / "project"
    git_project(root, {MANIFEST.name: COMMITTED, "inner/pyproject.toml": COMMITTED})

    with pytest.raises(ProjectSourceUnavailable, match="top level"):
        LocalGitProjectSource(root / "inner").head()


def test_a_directory_swapped_under_its_lease_is_never_unpacked_into(
    tmp_path: Path,
) -> None:
    """Unpacking happens in the window the lease identity exists to close."""

    root = tmp_path / "project"
    pin = git_project(root, {MANIFEST.name: COMMITTED})
    leased = lease(tmp_path)
    impostor = tmp_path / "impostor"
    impostor.mkdir()
    leased.working_directory.rmdir()
    impostor.rename(leased.working_directory)

    with pytest.raises(LeasedDirectoryChanged):
        LocalGitProjectSource(root).materialize(pin, leased)

    assert list(leased.working_directory.iterdir()) == []
    assert registered_worktrees(root) == {str(root)}


REPORT_THE_TREE = (
    "import json, os; "
    "print(json.dumps([sorted(os.listdir()), open('src/tool.py').read()]), end='')"
)


@dataclass
class TreeReportingExecutor(PrintModeExecutor):
    """A provider of no particular vendor, keeping what it was started in."""

    reported: list[bytes] = field(default_factory=list)

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(
            (sys.executable, "-c", REPORT_THE_TREE),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        del invocation
        self.reported.append(completion.standard_output)
        return AgentExecutionResult(completion.standard_output)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


@pytest.mark.proves("an-attempt-works-in-the-tree-its-own-binding-pinned")
def test_the_provider_starts_in_the_pinned_tree_of_its_own_lease(
    tmp_path: Path,
) -> None:
    """The whole point of the pin: the work happens on the material it named.

    The worktree pointer stands beside the pinned tree until the lease is
    detached; nothing between the lease and this provider detaches it here.
    """

    pin = git_project(
        tmp_path / "project",
        {MANIFEST.name: COMMITTED, "src/tool.py": "print('committed')\n"},
    )
    write_into_checkout(tmp_path / "project", {"src/tool.py": "print('later')\n"})
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    executor = TreeReportingExecutor()
    try:
        outcome = execute_agent_attempt(
            agent_attempt_execution(attempt_request(runtime, "pin/materialize")),
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
            declared_project(
                tmp_path / "project", runtime.settings.database_path
            ).pinned(pin, None),
            permissions=GRANTS_NOTHING,
            workspace_files=workspace_files_nobody_opens,
        )

        assert isinstance(outcome, AgentAttemptSucceeded)
        assert executor.reported == [
            json.dumps(
                [[".git", "pyproject.toml", "src"], "print('committed')\n"]
            ).encode()
        ]
    finally:
        runtime.close()


def durably_bound(**pinned: str) -> dict[str, object]:
    """A durable binding this adapter writes, changed only in what it pinned."""

    encoded = dict(
        encode_node_binding(AgentNodeBindingV2(resolved_agent_binding(), "build"))
    )
    return {**encoded, **pinned}


A_GRANT = {
    "tool_revision_hash": "c3" * 32,
    "tool_capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value,
}


@pytest.mark.parametrize(
    "half",
    [{"project_commit": "a1" * 20}, {"project_tree": "b2" * 20}],
    ids=["only the commit", "only the tree"],
)
@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_half_encoded_pin_refuses_rather_than_guessing_the_other_half(
    half: dict[str, str],
) -> None:
    with pytest.raises(RunBindingConflict, match="partly encoded"):
        decode_node_binding(durably_bound(**half))


@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_pin_that_names_no_objects_refuses_the_binding_that_carries_it() -> None:
    with pytest.raises(RunBindingConflict, match="unknown value"):
        decode_node_binding(
            durably_bound(project_commit="the tip", project_tree="b2" * 20)
        )


@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_grant_bound_without_a_pinned_source_refuses_by_what_is_missing() -> None:
    """A grant is redeemed against a tree, so a grant with no tree redeems nothing."""

    with pytest.raises(RunBindingConflict, match="pinned none"):
        decode_node_binding(durably_bound(**A_GRANT))


@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_pinned_source_no_runtime_was_given_refuses_rather_than_running_blind(
    tmp_path: Path,
) -> None:
    pin = git_project(tmp_path / "project", {MANIFEST.name: COMMITTED})
    binding = decode_node_binding(
        durably_bound(project_commit=pin.commit, project_tree=pin.tree)
    )

    assert isinstance(binding, AgentNodeBindingV2)
    with pytest.raises(RunBindingConflict, match="was given none"):
        pinned_project(binding, None)


def test_a_binding_pinning_a_source_and_no_grant_works_in_that_tree_and_redeems_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, {MANIFEST.name: COMMITTED})
    binding = decode_node_binding(
        durably_bound(project_commit=pin.commit, project_tree=pin.tree)
    )

    assert isinstance(binding, AgentNodeBindingV2)
    project = pinned_project(
        binding, declared_project(root, tmp_path / "atelier.sqlite")
    )

    assert project is not None
    assert (project.pin, project.grant) == (pin, None)
