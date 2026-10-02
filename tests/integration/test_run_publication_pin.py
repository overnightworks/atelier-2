"""Where a run's publishers stand: on the run's last publication, not on the head.

Every agent node used to pin the project's head when its binding was composed.
A publisher that ran after trunk moved would then commit the tree it inherited
onto a base that tree never saw, replace the branch head with it, and leave a
pull request that silently drops what trunk gained in between.

What is claimed here is a fact about commits, so every scenario that drives a
run builds one real repository whose head really moves, one bare remote the real
transport really pushes to, and one durable run whose receipts the reader really
reads -- the arrangement `tests/scenarios/publishing_runs.py` owns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.adapters.dbos.run_publications import (
    RunPublication,
    RunPublicationRefused,
    last_in_workflow_order,
)
from atelier2.adapters.project_source import LocalGitProjectSource
from tests.scenarios.publishing_runs import (
    BUILD,
    FIX,
    JOINING,
    LEFT,
    PUBLISHED_BRANCH,
    RIGHT,
    SEED,
    Publisher,
    publishing_run,
    workflow_graph,
)


def test_a_second_publisher_stands_on_the_base_of_the_first_though_trunk_moved(
    tmp_path: Path,
) -> None:
    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        started_on = LocalGitProjectSource(run.project).head()
        built = run.keep(BUILD, {"kept.txt": "what the builder made\n"})
        fixed = run.keep(FIX, {"fixed.txt": "what the fixer added\n"})
        run.forget_candidates()
        run.publish(BUILD, built, started_on.commit)
        moved_to = run.move_trunk({"trunk.txt": "what trunk gained beside the run\n"})

        pinned = run.pin_of(FIX)
        second = run.publish(FIX, fixed, pinned.commit)

        assert pinned == started_on
        assert pinned != moved_to
        assert run.parent_of(second) == started_on.commit
        assert run.changed_between(run.parent_of(second), second) == frozenset(
            {"kept.txt", "fixed.txt"}
        )


def test_the_only_publisher_of_a_run_stands_on_the_head(tmp_path: Path) -> None:
    with publishing_run(tmp_path, "Build alone", (BUILD,)) as run:
        moved_to = run.move_trunk({"trunk.txt": "what trunk gained\n"})

        assert run.pin_of(BUILD) == moved_to


@pytest.mark.parametrize(
    "replaced", [pytest.param(BUILD, id="first"), pytest.param(FIX, id="second")]
)
def test_a_replaced_publisher_stands_where_its_original_attempt_stood(
    tmp_path: Path, replaced: Publisher
) -> None:
    with publishing_run(tmp_path, "Build, then fix", (BUILD, FIX)) as run:
        started_on = LocalGitProjectSource(run.project).head()
        built = run.keep(BUILD, {"kept.txt": "what the builder made\n"})
        fixed = run.keep(FIX, {"fixed.txt": "what the fixer added\n"})
        run.forget_candidates()
        run.publish(BUILD, built, started_on.commit)
        run.move_trunk({"trunk.txt": "what trunk gained first\n"})
        run.publish(FIX, fixed, run.pin_of(FIX).commit)
        run.move_trunk({"later.txt": "what trunk gained after that\n"})

        assert run.pin_of(replaced) == started_on


def test_the_binding_of_a_replaced_attempt_carries_its_own_publication(
    tmp_path: Path,
) -> None:
    """The binding step itself, not the reader asked beside it, records the pin.

    A replacement attempt is composed by the same step as the original, so the
    node the run stands on is bound a second time after its push is confirmed
    and trunk has moved. What it records is what the attempt works in.
    """
    with publishing_run(tmp_path, "Build alone", (BUILD,)) as run:
        started_on = LocalGitProjectSource(run.project).head()
        built = run.keep(BUILD, {"kept.txt": "what the builder made\n"})
        run.forget_candidates()
        run.publish(BUILD, built, started_on.commit)
        moved_to = run.move_trunk({"trunk.txt": "what trunk gained beside the run\n"})

        binding = run.binding_of(BUILD)

        assert (binding.get("project_commit"), binding.get("project_tree")) == (
            started_on.commit,
            started_on.tree,
        )
        assert binding.get("project_commit") != moved_to.commit


def test_publications_the_workflow_never_ordered_are_refused_rather_than_guessed() -> (
    None
):
    """No pin at all where the graph orders two publishers only beside each other.

    Asked of the graph rather than of a started run, because this build starts
    only a line: the shape the reader must never guess over is one the document
    vocabulary already declares and the scheduler has not caught up with.
    """
    graph = workflow_graph(
        "Two publishers beside each other", (SEED, LEFT, RIGHT, JOINING)
    )
    beside_each_other = {
        publisher.node_id: RunPublication(
            PUBLISHED_BRANCH, "a1" * 20, "c3" * 20, publisher.attempt_id()
        )
        for publisher in (LEFT, RIGHT)
    }

    with pytest.raises(RunPublicationRefused, match="left, right"):
        last_in_workflow_order(graph, beside_each_other)
