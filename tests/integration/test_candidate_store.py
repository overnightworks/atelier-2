"""What an attempt made outlives the directory it made it in, or nothing claims it.

Every test here runs against real git repositories under the test's own temporary
directory and touches no network: what the store promises -- that a candidate is
still readable when the workspace and even the operator's checkout are gone -- is
only worth something if git itself says so.

Three properties carry the rest. The candidate is the whole material the pin
carries plus whatever the attempt did to it, so nothing is silently dropped and a
deletion is a deletion. One attempt names one piece of work: capturing the same
work twice is the same fact stated twice, capturing different work under the same
attempt is a contradiction and is refused. And nothing the machine around the run
says -- its git configuration, its environment, the hooks in either repository --
changes any of that or runs a line of anybody's script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from atelier2.adapters.candidate_store import (
    CANDIDATE_REF_PREFIX,
    CANDIDATE_STORE_DIRECTORY_NAME,
    LOCK_HANDOVER_PAUSE_SECONDS,
    PINNED_TREE_REF_PREFIX,
    GitCandidateTreeStore,
)
from atelier2.adapters.project_source import (
    LocalGitProjectSource,
    isolated_git_environment,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.project_sources import CandidateTree, GitObjectFormat
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.candidate_store import (
    CandidateCaptureConflict,
    CandidateNotKept,
    CandidateStoreUnavailable,
    CandidateTreeUnrepresentable,
    LeasedWorkingTree,
)
from tests.scenarios.agents import leased_directory_identity
from tests.scenarios.projects import commit_to_project, git_project

COMMITTED = "print('committed')\n"
WORKED_ON = "print('what the agent made')\n"
COMMITTED_LATER = "print('a second commit')\n"
KEPT_OUT_OF_GIT = "*.secret\n"
TRACKED_BUT_IGNORED = "the value a run must not lose\n"

AN_ATTEMPT = AgentAttemptId("a1" * 32)
ANOTHER_ATTEMPT = AgentAttemptId("b2" * 32)
A_THIRD_ATTEMPT = AgentAttemptId("c3" * 32)

HOSTILE_GLOBAL_CONFIGURATION = (
    '[filter "canary"]\n\tclean = sed s/./X/g\n\tsmudge = cat\n'
)
"""A host that rewrites content on the way into git, as git-lfs and hooks do."""

A_PROVIDER_TOKEN = "canary-token-no-git-child-may-see"
"""Stands for every credential a serving process happens to be holding."""

REGULAR_FILE_MODE = "100644"
EXECUTABLE_FILE_MODE = "100755"
SYMLINK_MODE = "120000"

BINARY_BODY = bytes(range(256)) * 4096
"""A megabyte with every byte value in it, so no text path can carry it by luck."""

WATCHING_FOR_THE_SEED_SECONDS = 0.005
WATCHING_GIVES_UP_AFTER_SECONDS = 10.0
HELD_PAST_THE_SEED_SECONDS = LOCK_HANDOVER_PAUSE_SECONDS
"""How the one test about a lost ref lock times its handover.

The lock is released relative to an event rather than to the clock: a new pack in
the store is the capture having just seeded its material, which is the breath
before it writes the ref and is refused. Holding on past that for one of the
store's own handover pauses keeps the lock there for the refusal itself, which
is the window under test, while leaving the rest of the store's own retry
budget as margin -- an independent guessed duration ate most of that budget and
flaked under coverage load (#747). A capture that never seeds at all ends the
watch rather than hanging the suite."""


class Project:
    """One checkout, its candidate store, and the workspaces an attempt leases."""

    def __init__(
        self,
        tmp_path: Path,
        files: dict[str, str],
        object_format: GitObjectFormat = GitObjectFormat.SHA1,
    ) -> None:
        self.checkout = tmp_path / "checkout"
        self.root = tmp_path / "project-root"
        self.root.mkdir()
        self.pin = git_project(self.checkout, files, object_format)
        self.store_path = self.root / CANDIDATE_STORE_DIRECTORY_NAME
        self.store = GitCandidateTreeStore(self.checkout, self.root / "atelier.sqlite")
        self._leases = 0

    def commit(self, files: dict[str, str]) -> None:
        """Take one more commit into the checkout and pin the run to that one."""

        self.pin = commit_to_project(self.checkout, files)

    def workspace(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        """A lease holding exactly the pinned tree, as a real attempt is given."""

        self._leases += 1
        lease = leased_directory_identity(
            attempt_id, self.checkout.parent / f"lease-{self._leases}"
        )
        LocalGitProjectSource(self.checkout).materialize(self.pin, lease)
        return lease

    def captured_at_once(
        self, leases: Iterable[AgentAttemptWorkspaceLease]
    ) -> tuple[CandidateTree, ...]:
        """Capture every one of these leases from its own thread, all at once."""

        racing = tuple(leases)
        ready = threading.Barrier(len(racing))

        def capture(lease: AgentAttemptWorkspaceLease) -> CandidateTree:
            ready.wait()
            return self.store.capture(self.pin, lease)

        with ThreadPoolExecutor(max_workers=len(racing)) as pool:
            return tuple(pool.map(capture, racing))


def asked_of_git(repository: Path, arguments: tuple[str, ...]) -> bytes:
    """What git itself says about a repository, asked outside the code under test.

    The reader is isolated the way the boundary isolates its own children, so a
    test asserts what a repository holds rather than what the machine running the
    test would have added to it.
    """

    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        env=isolated_git_environment(),
        capture_output=True,
        check=True,
    )
    return completed.stdout


def said_by_git(repository: Path, arguments: tuple[str, ...]) -> str:
    return asked_of_git(repository, arguments).decode("utf-8")


def carried(store_path: Path, tree: str) -> dict[str, str]:
    """Every path under this tree in the store, and the text each one holds."""

    listed = said_by_git(store_path, ("ls-tree", "-r", "--name-only", tree))
    return {
        path: said_by_git(store_path, ("cat-file", "-p", f"{tree}:{path}"))
        for path in listed.splitlines()
    }


def modes_in(store_path: Path, tree: str) -> dict[str, str]:
    """What kind of entry each path under this tree is, in git's own numbers."""

    listed = said_by_git(store_path, ("ls-tree", "-r", tree))
    return {
        line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in listed.splitlines()
    }


def bytes_under(store_path: Path, tree: str, path: str) -> bytes:
    return asked_of_git(store_path, ("cat-file", "-p", f"{tree}:{path}"))


def held_ref_lock(store_path: Path, reference: str) -> Path:
    """Take the file git creates while it writes this ref, so nobody else can."""

    lock = store_path / f"{reference}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    return lock


def publish_ref(held: Path, reference: Path, named: str) -> None:
    """Finish the ref write this lock was taken for, the way git finishes one.

    Filling the lock and renaming it into place rather than calling git, because
    a whole subprocess is slower than the window this is measuring.
    """

    held.write_text(f"{named}\n", encoding="utf-8")
    held.rename(reference)


def packs_in(store_path: Path) -> set[str]:
    """The packs this store holds; one more of them is a capture having seeded."""

    return {found.name for found in (store_path / "objects" / "pack").glob("*.pack")}


def every_file_under(root: Path) -> dict[str, bytes]:
    """Every file below this directory and what it holds, symlinks included."""

    return {
        str(found.relative_to(root)): (
            os.readlink(found).encode("utf-8")
            if found.is_symlink()
            else found.read_bytes()
        )
        for found in sorted(root.rglob("*"))
        if found.is_symlink() or found.is_file()
    }


def test_the_store_lives_in_the_project_root_beside_its_database(
    tmp_path: Path,
) -> None:
    """A project keeps nothing of its own outside its own root (ADR 0011)."""

    project = Project(tmp_path, {"tool.py": COMMITTED})

    project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert project.store_path.is_dir()


def test_capturing_leaves_the_operator_checkout_byte_for_byte_as_it_was(
    tmp_path: Path,
) -> None:
    """The checkout is read for what a run was pinned to, and never written."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")
    standing = every_file_under(project.checkout)

    project.store.capture(project.pin, lease)

    assert every_file_under(project.checkout) == standing


def test_the_captured_tree_is_the_workspace_down_to_what_the_attempt_deleted(
    tmp_path: Path,
) -> None:
    """A path the attempt removed is gone from the candidate, as in any commit."""

    project = Project(
        tmp_path,
        {"tool.py": COMMITTED, "kept.py": COMMITTED, "removed.py": COMMITTED},
    )
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")
    (lease.working_directory / "added.py").write_text(WORKED_ON, encoding="utf-8")
    (lease.working_directory / "removed.py").unlink()

    candidate = project.store.capture(project.pin, lease)

    assert candidate.attempt_id == AN_ATTEMPT
    assert carried(project.store_path, candidate.tree) == {
        "tool.py": WORKED_ON,
        "added.py": WORKED_ON,
        "kept.py": COMMITTED,
    }


def test_a_path_the_project_excludes_from_exports_is_still_material_to_work_on(
    tmp_path: Path,
) -> None:
    """`export-ignore` speaks about release archives, never about a working tree.

    A materialization that honoured it would hand the attempt less than it was
    pinned to, and the capture afterwards would read those paths as deleted.
    """

    project = Project(
        tmp_path,
        {
            ".gitattributes": "release-notes.md export-ignore\n",
            "release-notes.md": COMMITTED,
            "tool.py": COMMITTED,
        },
    )
    lease = project.workspace(AN_ATTEMPT)

    assert (lease.working_directory / "release-notes.md").exists()

    candidate = project.store.capture(project.pin, lease)

    assert carried(project.store_path, candidate.tree)["release-notes.md"] == COMMITTED


def test_the_candidate_carries_modes_and_binary_content_as_the_workspace_had_them(
    tmp_path: Path,
) -> None:
    """A candidate that lost a mode or a byte would not be the work that was done."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    (project.checkout / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (project.checkout / "run.sh").chmod(0o755)
    (project.checkout / "link").symlink_to("tool.py")
    (project.checkout / "blob.bin").write_bytes(BINARY_BODY)
    project.commit({})
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "made.bin").write_bytes(BINARY_BODY)

    candidate = project.store.capture(project.pin, lease)

    assert modes_in(project.store_path, candidate.tree) == {
        "blob.bin": REGULAR_FILE_MODE,
        "link": SYMLINK_MODE,
        "made.bin": REGULAR_FILE_MODE,
        "run.sh": EXECUTABLE_FILE_MODE,
        "tool.py": REGULAR_FILE_MODE,
    }
    assert bytes_under(project.store_path, candidate.tree, "made.bin") == BINARY_BODY
    assert bytes_under(project.store_path, candidate.tree, "blob.bin") == BINARY_BODY


def test_the_candidate_is_readable_when_workspace_and_checkout_are_gone(
    tmp_path: Path,
) -> None:
    """The store stands on itself: it is where the work of a run now lives."""

    project = Project(tmp_path, {"deep/nested/tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)

    candidate = project.store.capture(project.pin, lease)

    shutil.rmtree(project.checkout)
    shutil.rmtree(lease.working_directory)
    assert project.store.read(AN_ATTEMPT) == candidate
    assert carried(project.store_path, candidate.tree) == {
        "deep/nested/tool.py": COMMITTED
    }


def test_a_project_naming_its_objects_by_sha256_is_kept_in_its_own_format(
    tmp_path: Path,
) -> None:
    """An object's name is its hash, so a store of the other format holds nothing."""

    project = Project(
        tmp_path, {"tool.py": COMMITTED}, object_format=GitObjectFormat.SHA256
    )
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")

    candidate = project.store.capture(project.pin, lease)

    assert (
        said_by_git(project.store_path, ("rev-parse", "--show-object-format")).strip()
        == GitObjectFormat.SHA256.value
    )
    assert carried(project.store_path, candidate.tree) == {"tool.py": WORKED_ON}
    assert project.store.read(AN_ATTEMPT) == candidate


def test_a_file_the_project_tracks_but_ignores_survives_the_capture(
    tmp_path: Path,
) -> None:
    """The index is seeded from the pin, so ignored history is not lost silently."""

    project = Project(
        tmp_path, {"keys.secret": TRACKED_BUT_IGNORED, "tool.py": COMMITTED}
    )
    project.commit({".gitignore": KEPT_OUT_OF_GIT})

    candidate = project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert carried(project.store_path, candidate.tree)["keys.secret"] == (
        TRACKED_BUT_IGNORED
    )


def test_a_file_the_agent_newly_ignored_is_no_change_as_it_would_be_in_git(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, {".gitignore": KEPT_OUT_OF_GIT})
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "scratch.secret").write_text(WORKED_ON, encoding="utf-8")

    candidate = project.store.capture(project.pin, lease)

    assert carried(project.store_path, candidate.tree) == {
        ".gitignore": KEPT_OUT_OF_GIT
    }


def test_capturing_the_same_work_twice_states_the_same_candidate_twice(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)

    first = project.store.capture(project.pin, lease)
    again = project.store.capture(project.pin, lease)

    assert again == first
    assert project.store.read(AN_ATTEMPT) == first


def test_one_attempt_capturing_its_work_twice_at_once_states_it_once(
    tmp_path: Path,
) -> None:
    """A retry racing the capture it retried must not turn into a contradiction."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)

    raced = project.captured_at_once((lease, lease))

    assert raced[0] == raced[1] == project.store.read(AN_ATTEMPT)


def test_two_attempts_capturing_the_same_pin_at_once_each_keep_their_own_work(
    tmp_path: Path,
) -> None:
    """Both seed the same pinned material, and the loser of that race is not a loss."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    first = project.workspace(AN_ATTEMPT)
    second = project.workspace(ANOTHER_ATTEMPT)
    (first.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")

    raced = project.captured_at_once((first, second))

    assert {candidate.attempt_id for candidate in raced} == {
        AN_ATTEMPT,
        ANOTHER_ATTEMPT,
    }
    assert project.store.read(AN_ATTEMPT) == raced[0]
    assert project.store.read(ANOTHER_ATTEMPT) == raced[1]
    assert carried(project.store_path, raced[0].tree) == {"tool.py": WORKED_ON}
    assert carried(project.store_path, raced[1].tree) == {"tool.py": COMMITTED}


def test_a_capture_refused_the_ref_lock_waits_for_the_winner_and_keeps_its_work(
    tmp_path: Path,
) -> None:
    """Losing a ref lock to a writer of the same tree costs a capture nothing.

    git writes a ref by taking a `.lock` and renaming it into place. The lock is
    held here from before the capture starts until after it has seeded its
    material -- the breath before it writes that ref -- so the capture meets the
    refusal for real, and must still come away with its work rather than with an
    error about somebody else's write.
    """

    project = Project(tmp_path, {"tool.py": COMMITTED})
    project.store.capture(project.pin, project.workspace(A_THIRD_ATTEMPT))
    project.commit({"tool.py": COMMITTED_LATER})
    rooted_at = PINNED_TREE_REF_PREFIX + project.pin.tree
    held = held_ref_lock(project.store_path, rooted_at)
    lease = project.workspace(ANOTHER_ATTEMPT)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")
    seeded_before = packs_in(project.store_path)
    ready = threading.Barrier(2)

    def hand_over() -> None:
        ready.wait()
        watched_until = time.monotonic() + WATCHING_GIVES_UP_AFTER_SECONDS
        while (
            packs_in(project.store_path) == seeded_before
            and time.monotonic() < watched_until
        ):
            time.sleep(WATCHING_FOR_THE_SEED_SECONDS)
        time.sleep(HELD_PAST_THE_SEED_SECONDS)
        publish_ref(held, project.store_path / rooted_at, project.pin.tree)

    with ThreadPoolExecutor(max_workers=1) as pool:
        handing = pool.submit(hand_over)
        ready.wait()
        candidate = project.store.capture(project.pin, lease)
        handing.result()

    assert project.store.read(ANOTHER_ATTEMPT) == candidate
    assert carried(project.store_path, candidate.tree) == {"tool.py": WORKED_ON}


def test_a_pinned_tree_rooted_at_other_material_is_refused_and_left_standing(
    tmp_path: Path,
) -> None:
    """A ref names its own tree, so a ref standing elsewhere is a lie, not a stale value.

    Overwriting it would keep the lie and hand the next capture material that is
    not what its pin says it is.
    """

    project = Project(tmp_path, {"tool.py": COMMITTED})
    first = project.workspace(AN_ATTEMPT)
    (first.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")
    other = project.store.capture(project.pin, first)
    rooted_at = PINNED_TREE_REF_PREFIX + project.pin.tree
    asked_of_git(project.store_path, ("update-ref", rooted_at, other.tree))

    with pytest.raises(CandidateStoreUnavailable, match=other.tree):
        project.store.capture(project.pin, project.workspace(ANOTHER_ATTEMPT))

    assert (
        said_by_git(
            project.store_path, ("for-each-ref", "--format=%(objectname)", rooted_at)
        ).strip()
        == other.tree
    )
    assert project.store.read(ANOTHER_ATTEMPT) is None


def test_one_attempt_claiming_other_work_is_refused_rather_than_overwritten(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    anchored = project.store.capture(project.pin, lease)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")

    with pytest.raises(CandidateCaptureConflict, match=anchored.tree):
        project.store.capture(project.pin, lease)

    assert project.store.read(AN_ATTEMPT) == anchored


def test_a_nested_repository_is_refused_before_any_candidate_is_written(
    tmp_path: Path,
) -> None:
    """A gitlink names a commit this store has never seen, so it names nothing."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    git_project(lease.working_directory / "vendored", {"other.py": COMMITTED})

    with pytest.raises(CandidateTreeUnrepresentable, match="vendored"):
        project.store.capture(project.pin, lease)

    assert project.store.read(AN_ATTEMPT) is None


def test_a_directory_swapped_under_its_lease_is_never_captured_from(
    tmp_path: Path,
) -> None:
    """Capturing happens in the same window the lease identity exists to close.

    The swap is refused as a candidate that was not kept, not as the raw lease
    error underneath it: this store is asked between an attempt's last work and
    its completion, and every way it can fail has to reach that caller in the
    one shape it catches, or the attempt is left armed.
    """

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    impostor = tmp_path / "impostor"
    impostor.mkdir()
    shutil.rmtree(lease.working_directory)
    impostor.rename(lease.working_directory)

    with pytest.raises(CandidateNotKept):
        project.store.capture(project.pin, lease)

    assert project.store.read(AN_ATTEMPT) is None


def test_a_store_that_is_no_repository_refuses_instead_of_claiming_a_candidate(
    tmp_path: Path,
) -> None:
    """No candidate is ever half kept: either the store answers, or the run hears."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    project.store_path.write_text("not a repository", encoding="utf-8")

    with pytest.raises(CandidateStoreUnavailable, match=str(project.store_path)):
        project.store.capture(project.pin, lease)


def test_reading_a_store_that_is_no_directory_refuses_rather_than_saying_nothing(
    tmp_path: Path,
) -> None:
    """A project whose kept work is corrupt is not a project whose attempt made none."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    project.store_path.write_text("not a repository", encoding="utf-8")

    with pytest.raises(CandidateStoreUnavailable, match=str(project.store_path)):
        project.store.read(AN_ATTEMPT)


@pytest.mark.parametrize(
    "a_repository_stands_there",
    [True, False],
    ids=["a real bare repository outside the root", "nothing at all"],
)
def test_a_store_reached_through_a_symbolic_link_is_refused_on_both_ways(
    tmp_path: Path, a_repository_stands_there: bool
) -> None:
    """A project keeps what its attempts made in its own root, not where a link points.

    Following the link would put every candidate somewhere the root does not own,
    and a link pointing nowhere would answer "this attempt captured nothing" for
    work that was never kept at all.
    """

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    elsewhere = tmp_path / "outside-the-root"
    if a_repository_stands_there:
        asked_of_git(project.checkout, ("init", "--bare", "--quiet", str(elsewhere)))
    project.store_path.symlink_to(elsewhere)

    with pytest.raises(CandidateStoreUnavailable, match=str(project.store_path)):
        project.store.capture(project.pin, lease)
    with pytest.raises(CandidateStoreUnavailable, match=str(project.store_path)):
        project.store.read(AN_ATTEMPT)

    assert not (elsewhere / "refs" / "atelier").exists()


def test_a_store_naming_objects_the_other_way_refuses_before_it_is_written_into(
    tmp_path: Path,
) -> None:
    """A store of the other hash cannot hold one object this project ever made."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    subprocess.run(
        (
            "git",
            "init",
            "--bare",
            "--quiet",
            f"--object-format={GitObjectFormat.SHA256.value}",
            str(project.store_path),
        ),
        env=isolated_git_environment(),
        capture_output=True,
        check=True,
    )

    with pytest.raises(CandidateStoreUnavailable, match=GitObjectFormat.SHA256.value):
        project.store.capture(project.pin, lease)

    assert project.store.read(AN_ATTEMPT) is None


def test_no_candidate_is_claimed_for_an_attempt_that_captured_none(
    tmp_path: Path,
) -> None:
    """Before the first capture there is no store yet, and that is an answer too."""

    project = Project(tmp_path, {"tool.py": COMMITTED})

    assert project.store.read(ANOTHER_ATTEMPT) is None

    project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert project.store.read(ANOTHER_ATTEMPT) is None


def test_the_host_git_configuration_cannot_rewrite_what_a_candidate_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `clean` filter of the machine is no part of any project's work."""

    project = Project(
        tmp_path, {".gitattributes": "* filter=canary\n", "tool.py": COMMITTED}
    )
    hostile = tmp_path / "hostile-gitconfig"
    hostile.write_text(HOSTILE_GLOBAL_CONFIGURATION, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    assert _would_be_rewritten(project.checkout), "the canary filter never fired"

    candidate = project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert carried(project.store_path, candidate.tree)["tool.py"] == COMMITTED


def _would_be_rewritten(checkout: Path) -> bool:
    """Whether the ambient configuration changes content on its way into git."""

    hashed = tuple(
        subprocess.run(
            (
                "git",
                "-C",
                str(checkout),
                "hash-object",
                "--stdin",
                *path_arguments,
            ),
            input=COMMITTED.encode("utf-8"),
            env={**os.environ, "GIT_CONFIG_SYSTEM": os.devnull},
            capture_output=True,
            check=True,
        ).stdout
        for path_arguments in ((), ("--path", "tool.py"))
    )
    return hashed[0] != hashed[1]


def hostile_environment(tmp_path: Path) -> dict[str, str]:
    """Every way a serving process's environment could reach into a git child.

    Each of these names is a real lever: where objects are written, which index is
    staged, under which namespace refs land, which program is asked for a
    password, where git writes a trace of everything it did -- and, last, a
    credential that has no business leaving this process at all.
    """

    return {
        "GIT_DIR": str(tmp_path / "hijacked-git-dir"),
        "GIT_WORK_TREE": str(tmp_path / "hijacked-work-tree"),
        "GIT_INDEX_FILE": str(tmp_path / "hijacked-index"),
        "GIT_OBJECT_DIRECTORY": str(tmp_path / "hijacked-objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(tmp_path / "hijacked-alternates"),
        "GIT_COMMON_DIR": str(tmp_path / "hijacked-common-dir"),
        "GIT_NAMESPACE": "hijacked",
        "GIT_TEMPLATE_DIR": str(tmp_path / "hijacked-template"),
        "GIT_ASKPASS": str(tmp_path / "hijacked-askpass"),
        "GIT_TRACE": str(tmp_path / "hijacked-trace.log"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "hijacked-hooks"),
        "GITHUB_TOKEN": A_PROVIDER_TOKEN,
    }


def test_no_variable_of_the_serving_process_reaches_a_git_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment is built from a list, so a token cannot travel by accident."""

    hostile = hostile_environment(tmp_path)
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    handed = isolated_git_environment()

    assert A_PROVIDER_TOKEN not in "\n".join(handed.values())
    assert [name for name, said in hostile.items() if handed.get(name) == said] == []


def test_a_capture_under_a_hostile_environment_keeps_the_work_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the machine around a run says cannot move where its work is kept."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")
    hostile = hostile_environment(tmp_path)
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    candidate = project.store.capture(project.pin, lease)

    assert carried(project.store_path, candidate.tree) == {"tool.py": WORKED_ON}
    assert said_by_git(
        project.store_path, ("for-each-ref", "--format=%(refname)")
    ).splitlines() == [
        CANDIDATE_REF_PREFIX + AN_ATTEMPT.value,
        PINNED_TREE_REF_PREFIX + project.pin.tree,
    ]
    assert not any(Path(hostile[name]).exists() for name in _HIJACKED_PATH_NAMES)


_HIJACKED_PATH_NAMES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_TRACE",
)
"""The hostile names whose whole effect would be a file or directory appearing."""


REWRITING_HOOK = f"#!/bin/sh\nprintf %s '{WORKED_ON}' > hooked.py\nexit 1\n"
"""A hook that both changes the work and refuses it, so either effect is visible."""

CHECKOUT_HOOK_NAMES = ("pre-push", "post-checkout", "reference-transaction")
STORE_HOOK_NAMES = ("pre-receive", "update", "post-receive", "reference-transaction")


def install_hooks(git_directory: Path, names: Iterable[str]) -> None:
    hooks = git_directory / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in names:
        hook = hooks / name
        hook.write_text(REWRITING_HOOK, encoding="utf-8")
        hook.chmod(0o755)


def test_no_hook_of_either_repository_runs_during_a_capture(tmp_path: Path) -> None:
    """A run does the work it was asked to do, and not a line of anybody's script.

    The hooks here would refuse the transaction outright and write a file into
    whatever directory they ran in, so a capture that let one fire could neither
    succeed nor leave the checkout as it found it.
    """

    project = Project(tmp_path, {"tool.py": COMMITTED})
    project.store.capture(project.pin, project.workspace(AN_ATTEMPT))
    install_hooks(project.checkout / ".git", CHECKOUT_HOOK_NAMES)
    install_hooks(project.store_path, STORE_HOOK_NAMES)
    standing = every_file_under(project.checkout)
    lease = project.workspace(ANOTHER_ATTEMPT)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")

    candidate = project.store.capture(project.pin, lease)

    assert carried(project.store_path, candidate.tree) == {"tool.py": WORKED_ON}
    assert every_file_under(project.checkout) == standing
    assert not (lease.working_directory / "hooked.py").exists()


def test_a_created_store_carries_no_hooks_of_its_own(tmp_path: Path) -> None:
    """`git init` copies a template, and the machine's template is not this product's."""

    project = Project(tmp_path, {"tool.py": COMMITTED})

    project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert not (project.store_path / "hooks").exists()


def test_a_candidate_tree_names_a_real_object_or_is_no_candidate_at_all() -> None:
    """What git answered is read at a boundary, so a non-answer never becomes one."""

    with pytest.raises(ValueError, match="candidate tree"):
        CandidateTree(AN_ATTEMPT, "the work the agent did")


WARNING_A_REPOSITORY_MAKES_GIT_REPEAT = (
    b"warning: this .gitattributes line names something git does not know\n"
)
WARNINGS_PAST_ONE_PIPE_BUFFER = 4_000
"""More standard error than any pipe holds, so a child writing it has to block."""

THE_PATCH_A_FLOODING_GIT_STILL_PRINTS = b"diff --git a/tool.py b/tool.py\n"
A_FLOODING_GIT_GIVES_UP_AFTER_SECONDS = 20
"""What keeps a deadlock a failing test rather than a suite that never ends."""

A_GIT_THAT_FLOODS_ITS_STANDARD_ERROR = f"""#!{sys.executable}
import signal
import sys

signal.alarm({A_FLOODING_GIT_GIVES_UP_AFTER_SECONDS})
sys.stderr.buffer.write(
    {WARNING_A_REPOSITORY_MAKES_GIT_REPEAT!r} * {WARNINGS_PAST_ONE_PIPE_BUFFER}
)
sys.stderr.buffer.flush()
sys.stdout.buffer.write({THE_PATCH_A_FLOODING_GIT_STILL_PRINTS!r})
"""
"""A git that says a great deal before it answers, as a warned-about tree does."""


def test_a_git_flooding_its_standard_error_still_answers_a_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The patch is read under a bound, so nothing else may be a pipe nobody drains.

    How much git warns is decided by the tree it reads -- one `.gitattributes`
    line git does not understand is warned about once per path it touches -- and
    a bounded read stops taking standard output while the child is still
    running. A child filling a standard-error pipe the parent is not draining
    blocks on that write for as long as the parent waits, which is forever.
    """

    project = Project(tmp_path, {"tool.py": COMMITTED})
    on_the_path = tmp_path / "a-flooding-git"
    on_the_path.mkdir()
    flooding = on_the_path / "git"
    flooding.write_text(A_GIT_THAT_FLOODS_ITS_STANDARD_ERROR, encoding="utf-8")
    flooding.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_the_path))

    patch = project.store.changes(LeasedWorkingTree(project.pin, project.pin.tree))

    assert patch.read == THE_PATCH_A_FLOODING_GIT_STILL_PRINTS
    assert patch.stopped_at_the_bound is False
