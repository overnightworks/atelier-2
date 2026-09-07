"""The claim door's checkout: a clean linked worktree on the lane branch at the pin.

Every fact here is one the claim ledger checks before it acts, read back through
git itself on temporary repositories. The provider's lease is not this checkout
and is not touched by these tests.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from atelier2.adapters.claim_checkouts import (
    ClaimCheckoutRootRefused,
    LocalClaimCheckouts,
)
from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.runs import RunId
from atelier2.ports.claim_checkouts import (
    ClaimCheckoutRefused,
    ClaimCheckoutUnavailable,
)
from tests.scenarios.projects import (
    commit_to_project,
    declared_in_checkout,
    git_project,
    run_git,
)

A_PROJECT = {
    "README.md": "read me\n",
    "src/tool.py": "print('committed')\n",
    "docs/guide.md": "guide\n",
}
LANE = HeadBranch("atelier2/work-item/claim")
A_RUN = RunId("runs/with a slash and spaces")
ANOTHER_RUN = RunId("another")
POISONED_SMUDGE = "sed s/./X/g"


class Project:
    """One temporary checkout and the claim checkouts opened from it."""

    def __init__(self, tmp_path: Path, files: dict[str, str] = A_PROJECT) -> None:
        self.checkout = tmp_path / "project"
        self.pin = git_project(self.checkout, files)
        self.root = tmp_path / "claim-checkouts"
        self.checkouts = LocalClaimCheckouts(self.checkout, self.root)

    def registered_worktrees(self) -> set[str]:
        listed = run_git(self.checkout, "worktree", "list", "--porcelain")
        return {
            line.removeprefix("worktree ")
            for line in listed.splitlines()
            if line.startswith("worktree ")
        }

    def state(self) -> tuple[str, str, str]:
        """What the project checkout stands on: HEAD, its index, and `main`."""

        return (
            run_git(self.checkout, "rev-parse", "HEAD"),
            run_git(self.checkout, "write-tree"),
            run_git(self.checkout, "rev-parse", "refs/heads/main"),
        )

    def lane_ref(self) -> str:
        """The commit the lane branch stands at, or nothing while there is none."""

        return run_git(
            self.checkout, "for-each-ref", "--format=%(objectname)", LANE.full_ref
        )

    def sparse_to(self, pattern: str) -> None:
        """Leave the source holding only `pattern`, as an operator's checkout may."""

        declared_in_checkout(self.checkout, {"core.sparseCheckout": "true"})
        (self.checkout / ".git" / "info" / "sparse-checkout").write_text(
            f"{pattern}\n", encoding="utf-8"
        )
        run_git(self.checkout, "read-tree", "-mu", "HEAD")

    def hook_canary(self) -> Path:
        """A post-checkout hook that leaves a file wherever it ran."""

        fired = self.checkout / "hook-fired"
        hook = self.checkout / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f'#!/bin/sh\ntouch "{fired}"\n', encoding="utf-8")
        hook.chmod(0o755)
        return fired


def worktree_facts(working_directory: Path) -> dict[str, str]:
    """What the claim ledger reads of a checkout before it acts."""

    return {
        "git_dir": run_git(working_directory, "rev-parse", "--git-dir"),
        "common_dir": run_git(working_directory, "rev-parse", "--git-common-dir"),
        "branch": run_git(working_directory, "branch", "--show-current"),
        "head": run_git(working_directory, "rev-parse", "HEAD"),
        "status": run_git(working_directory, "status", "--porcelain"),
    }


def tracked_files(working_directory: Path) -> set[str]:
    return set(run_git(working_directory, "ls-files").splitlines())


def test_open_makes_a_clean_linked_worktree_on_the_lane_branch_at_the_pin(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)

    opened = project.checkouts.open(A_RUN, LANE, project.pin)

    facts = worktree_facts(opened)
    assert facts["git_dir"] != facts["common_dir"]
    assert (facts["branch"], facts["head"], facts["status"]) == (
        LANE.value,
        project.pin.commit,
        "",
    )
    assert tracked_files(opened) == set(A_PROJECT)
    assert opened.parent == project.root
    assert project.registered_worktrees() == {str(project.checkout), str(opened)}


def test_a_sparse_source_still_yields_the_whole_pin(tmp_path: Path) -> None:
    """A linked worktree inherits the source's sparse pattern; the pin does not."""

    project = Project(tmp_path)
    project.sparse_to("/src/")
    assert not (project.checkout / "README.md").exists()

    opened = project.checkouts.open(A_RUN, LANE, project.pin)

    assert {name for name in A_PROJECT if (opened / name).is_file()} == set(A_PROJECT)
    assert worktree_facts(opened)["status"] == ""


def test_no_hook_of_the_source_runs_while_a_checkout_is_made(tmp_path: Path) -> None:
    project = Project(tmp_path)
    fired = project.hook_canary()

    opened = project.checkouts.open(A_RUN, LANE, project.pin)

    assert not fired.exists()
    assert not (opened / fired.name).exists()


def test_a_filter_driver_the_source_declares_refuses_before_anything_is_made(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)
    declared_in_checkout(
        project.checkout,
        {"filter.poison.smudge": POISONED_SMUDGE, "filter.poison.clean": "cat"},
    )

    with pytest.raises(ClaimCheckoutUnavailable, match="filter.poison.smudge"):
        project.checkouts.open(A_RUN, LANE, project.pin)

    assert not project.root.exists()
    assert project.registered_worktrees() == {str(project.checkout)}
    assert project.lane_ref() == ""


def test_a_second_open_of_one_run_finds_the_standing_checkout(tmp_path: Path) -> None:
    project = Project(tmp_path)
    first = project.checkouts.open(A_RUN, LANE, project.pin)

    second = project.checkouts.open(A_RUN, LANE, project.pin)

    assert second == first
    assert project.registered_worktrees() == {str(project.checkout), str(first)}


def test_a_standing_checkout_at_another_pin_is_not_this_runs(tmp_path: Path) -> None:
    """Found again means found as it was opened: same branch, same pin."""

    project = Project(tmp_path)
    earlier = project.pin
    project.checkouts.open(A_RUN, LANE, earlier)
    later = commit_to_project(project.checkout, {"README.md": "changed\n"})

    with pytest.raises(ClaimCheckoutRefused, match=later.commit):
        project.checkouts.open(A_RUN, LANE, later)


def a_plain_directory(project: Project, path: Path) -> None:
    path.mkdir()
    (path / ".env").write_text("the operator's own secret", encoding="utf-8")


def a_link_to_the_project_checkout(project: Project, path: Path) -> None:
    path.symlink_to(project.checkout)


def a_plain_clone(project: Project, path: Path) -> None:
    run_git(project.checkout, "clone", "--quiet", str(project.checkout), str(path))


def a_worktree_of_another_repository(project: Project, path: Path) -> None:
    other = project.checkout.parent / "other"
    pin = git_project(other, {"other.txt": "other\n"})
    run_git(
        other, "worktree", "add", "--quiet", "-B", LANE.value, str(path), pin.commit
    )


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file under the directory, its repository administration included."""

    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def working_tree(directory: Path) -> dict[str, bytes]:
    """The files a person works on there; the lane ref beneath `.git` is not one."""

    return {
        name: body
        for name, body in snapshot(directory).items()
        if not name.startswith(".git/")
    }


@pytest.mark.parametrize(
    ("standing", "refused_as"),
    [
        (a_plain_directory, "no checkout"),
        (a_link_to_the_project_checkout, "a link or the project checkout"),
        (a_plain_clone, "not a linked worktree"),
        (a_worktree_of_another_repository, "not a linked worktree"),
    ],
    ids=["plain directory", "link to the checkout", "clone", "foreign worktree"],
)
def test_what_is_not_this_runs_checkout_is_refused_untouched(
    tmp_path: Path,
    standing: Callable[[Project, Path], None],
    refused_as: str,
) -> None:
    """Found again means a linked worktree of this checkout, never what else stands."""

    project = Project(tmp_path)
    occupied = project.checkouts.open(A_RUN, LANE, project.pin)
    project.checkouts.close(A_RUN)
    standing(project, occupied)
    before = (working_tree(project.checkout), snapshot(occupied), project.state())

    with pytest.raises(ClaimCheckoutRefused, match=refused_as):
        project.checkouts.open(A_RUN, LANE, project.pin)

    assert (
        working_tree(project.checkout),
        snapshot(occupied),
        project.state(),
    ) == before


def test_a_lane_branch_a_standing_checkout_holds_refuses_the_next_run(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)
    project.checkouts.open(A_RUN, LANE, project.pin)

    with pytest.raises(ClaimCheckoutUnavailable, match=LANE.value):
        project.checkouts.open(ANOTHER_RUN, LANE, project.pin)

    assert len(project.registered_worktrees()) == 2


def test_close_removes_the_checkout_and_its_administration_and_keeps_the_lane_ref(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)
    opened = project.checkouts.open(A_RUN, LANE, project.pin)
    (opened / "left-behind.txt").write_text("dirty", encoding="utf-8")

    project.checkouts.close(A_RUN)
    project.checkouts.close(A_RUN)

    assert not opened.exists()
    assert not (project.checkout / ".git" / "worktrees").exists()
    assert project.registered_worktrees() == {str(project.checkout)}
    assert project.lane_ref() == project.pin.commit


def test_closing_one_run_leaves_another_runs_vanished_checkout_registered(
    tmp_path: Path,
) -> None:
    """A locked entry survives the prune; only its own run's close takes it."""

    project = Project(tmp_path)
    first = project.checkouts.open(A_RUN, LANE, project.pin)
    second = project.checkouts.open(
        ANOTHER_RUN, HeadBranch("atelier2/work-item/other"), project.pin
    )
    shutil.rmtree(first)

    project.checkouts.close(ANOTHER_RUN)

    assert project.registered_worktrees() == {str(project.checkout), str(first)}
    assert not second.exists()

    project.checkouts.close(A_RUN)

    assert project.registered_worktrees() == {str(project.checkout)}


def test_open_and_close_leave_the_project_checkout_where_it_stood(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)
    (project.checkout / "README.md").write_text("edited, unstaged\n", encoding="utf-8")
    run_git(project.checkout, "add", "README.md")
    before = (project.state(), working_tree(project.checkout))

    project.checkouts.open(A_RUN, LANE, project.pin)
    project.checkouts.close(A_RUN)

    assert (project.state(), working_tree(project.checkout)) == before


def test_a_root_inside_the_project_checkout_is_refused(tmp_path: Path) -> None:
    project = Project(tmp_path)

    with pytest.raises(ClaimCheckoutRootRefused, match="inside"):
        LocalClaimCheckouts(project.checkout, project.checkout / "claims")


def test_an_existing_root_is_held_at_mode_700(tmp_path: Path) -> None:
    project = Project(tmp_path)
    project.root.mkdir(mode=0o755)

    project.checkouts.open(A_RUN, LANE, project.pin)

    assert stat.S_IMODE(os.stat(project.root).st_mode) == 0o700


def test_a_closed_run_opens_again_at_the_pin(tmp_path: Path) -> None:
    """`-B` resets the lane to the pin, so a released lane starts where it was pinned."""

    project = Project(tmp_path)
    opened = project.checkouts.open(A_RUN, LANE, project.pin)
    run_git(opened, "commit", "--quiet", "--allow-empty", "--message", "moved on")
    project.checkouts.close(A_RUN)

    reopened = project.checkouts.open(A_RUN, LANE, project.pin)

    assert worktree_facts(reopened)["head"] == project.pin.commit


def test_a_missing_pin_makes_nothing(tmp_path: Path) -> None:
    project = Project(tmp_path)
    lost = ProjectSourcePin("f0" * 20, "e1" * 20)

    with pytest.raises(ClaimCheckoutUnavailable, match=lost.commit):
        project.checkouts.open(A_RUN, LANE, lost)

    assert project.lane_ref() == ""
