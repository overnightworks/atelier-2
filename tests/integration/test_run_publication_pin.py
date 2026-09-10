"""Where a run's publishers stand: on the run's last publication, not on the head.

Every agent node used to pin the project's head when its binding was composed.
A publisher that ran after trunk moved would then commit the tree it inherited
onto a base that tree never saw, replace the branch head with it, and leave a
pull request that silently drops what trunk gained in between.

What is claimed here is a fact about commits, so every scenario that drives a
run builds one real repository whose head really moves, one bare remote the real
transport really pushes to, and one durable run whose receipts the reader really
reads.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from atelier2.adapters.dbos.advancer import prepared_effect_intent
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.node_binding_codec import EncodedAgentBindingV2
from atelier2.adapters.dbos.run_publications import (
    NodeInRun,
    RunPublication,
    RunPublicationRefused,
    last_in_workflow_order,
    pinned_source_for,
)
from atelier2.adapters.dbos.run_transitions import load_graph
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow import _node_binding
from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.adapters.project_verification import declared_project
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    GitCommitIdentity,
    HeadBranch,
    PushAtelierCommit,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    PerformedEffect,
)
from atelier2.contracts.executions import logical_effect_key_for_node
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, RunId, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.projects import commit_to_project, git_project, run_git
from tests.scenarios.runs import publish_pinned_revisions, start_published_v3_run
from tests.scenarios.runtime import recording_exact_runtime
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/publishers-of-one-run")


@dataclass(frozen=True, slots=True)
class Publisher:
    """One node of the scenario's line: what it depends on, and whether it pushes."""

    node_id: str
    depends_on: tuple[str, ...] = ()
    publishes: bool = True

    def attempt_id(self) -> str:
        """The attempt id this node's candidate is kept under, distinct per node."""
        return self.node_id.encode().hex().ljust(64, "0")[:64]


BUILD = Publisher("build")
FIX = Publisher("fix", depends_on=("build",))
SEED = Publisher("seed", publishes=False)
LEFT = Publisher("left", depends_on=("seed",))
RIGHT = Publisher("right", depends_on=("seed",))
JOINING = Publisher("join", depends_on=("left", "right"))


@dataclass(frozen=True, slots=True)
class PublishingRun:
    """A started run over a real project repository, its remote and its candidates."""

    runtime: DbosRuntime
    revision: WorkflowRevision
    project: Path
    remote: Path
    candidates: Path
    transport: GitTransportEffectAdapterFactory

    def keep(self, publisher: Publisher, files: Mapping[str, str]) -> str:
        """Keep a candidate: these files over what the checkout already carries.

        Written over rather than beside, so calling this twice keeps the second
        attempt continuing the first one's work exactly as an inherited
        candidate does.
        """
        for name, body in files.items():
            self.project.joinpath(name).write_text(body, encoding="utf-8")
        run_git(self.project, "add", "--all")
        tree = run_git(self.project, "write-tree")
        holder = run_git(self.project, "commit-tree", tree, "-m", "kept")
        run_git(self.candidates, "fetch", "--quiet", str(self.project), holder)
        run_git(
            self.candidates,
            "update-ref",
            f"refs/atelier/candidates/{publisher.attempt_id()}",
            tree,
        )
        return tree

    def forget_candidates(self) -> None:
        """Put the checkout back on its own head, keeping every kept tree readable."""
        run_git(self.project, "reset", "--hard", "--quiet")
        run_git(self.project, "clean", "-fdq")

    def move_trunk(self, files: Mapping[str, str]) -> ProjectSourcePin:
        """Land one commit on trunk, here and on the remote, and pin what results."""
        pin = commit_to_project(self.project, files)
        run_git(
            self.project, "push", "--quiet", str(self.remote), "HEAD:refs/heads/main"
        )
        return pin

    def publish(self, publisher: Publisher, tree: str, base: str) -> str:
        """Push this node's kept tree for real, and confirm the receipt it earns."""
        intent = self._push_intent(publisher, tree, base)
        adapter = self.transport.open()
        try:
            performed = adapter.execute(intent)
        finally:
            adapter.close()
        assert isinstance(performed, PerformedEffect), performed
        with canonical_write_transaction(self.runtime.engine) as connection:
            prepared_effect_intent(connection, intent)
            commit_resolution(
                connection,
                intent.binding.logical_key.value,
                self.revision.revision_hash.value,
                encode_found(performed, ConfirmationSource.ADAPTER_EXECUTION),
            )
        return performed.effect_id.value

    def binding_of(self, publisher: Publisher) -> EncodedAgentBindingV2:
        """What the run's own binding step records for the node it stands on."""
        encoded = _node_binding(
            self.runtime.datasource,
            RUN,
            self.revision.revision_hash,
            publisher.node_id,
            declared_project(self.project, self.runtime.settings.database_path),
        )
        return cast(EncodedAgentBindingV2, encoded)

    def pin_of(self, publisher: Publisher) -> ProjectSourcePin:
        """What this node's binding would pin, asked exactly as the binding asks."""
        with self.runtime.engine.begin() as connection:
            return pinned_source_for(
                connection,
                load_graph(connection, self.revision.revision_hash),
                NodeInRun(
                    RUN,
                    self.revision.revision_hash,
                    publisher.node_id,
                    FIRST_ROUND_ORDINAL,
                ),
                LocalGitProjectSource(self.project),
            )

    def parent_of(self, commit: str) -> str:
        return run_git(self.remote, "rev-parse", f"{commit}^")

    def changed_between(self, earlier: str, later: str) -> frozenset[str]:
        listing = run_git(self.remote, "diff", "--name-only", earlier, later)
        return frozenset(listing.splitlines())

    def _push_intent(self, publisher: Publisher, tree: str, base: str) -> EffectIntent:
        request = PushAtelierCommit(
            publisher.attempt_id(),
            tree,
            base,
            HeadBranch("atelier2/work-item/" + "b2" * 32),
            GitCommitIdentity("Atelier Agent", "agent@example.test"),
            GitCommitIdentity("Atelier Core", "core@example.test"),
            "2026-09-10T12:00:00Z",
        )
        return EffectIntent(
            EffectBinding(
                logical_effect_key_for_node(
                    RUN,
                    self.revision.revision_hash,
                    publisher.node_id,
                    FIRST_ROUND_ORDINAL,
                ),
                RUN,
                self.revision.revision_hash,
                self.transport.binding.adapter_revision,
                self.transport.binding.destination,
                self.transport.binding.operational_identity,
                AdapterOperationName.PUSH_ATELIER_COMMIT,
            ),
            CanonicalRequest(request.canonical_bytes()),
        )


def _push_operation() -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        (
            '{"author":{"email":"agent@example.test","name":"Atelier Agent"},'
            '"committer":{"email":"core@example.test","name":"Atelier Core"},'
            f'"operation":"{AdapterOperationName.PUSH_ATELIER_COMMIT.value}"}}'
        ).encode(),
    )


def _push_grant(operation: PublishedRevision) -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.TOOL,
        (
            f'{{"capability":"{ToolGrantCapability.PUSH_ATELIER_COMMIT.value}",'
            f'"operation":{{"ref":"push-atelier-commit",'
            f'"revision":"{operation.revision_hash.value}"}}}}'
        ).encode(),
    )


def _document(
    name: str, publishers: tuple[Publisher, ...], grant: PublishedRevision
) -> bytes:
    document = f"format_version: 3\nname: {name}\nnodes:\n".encode()
    for publisher in publishers:
        document += f"""  - id: {publisher.node_id}
    type: agent
    role: builder
    mode: headless
    instruction: Carry this run one step further.
""".encode()
        if publisher.depends_on:
            document += (
                f"    depends_on: [{', '.join(publisher.depends_on)}]\n".encode()
            )
        if len(publisher.depends_on) > 1:
            document += b"    join: all_succeeded\n"
        if publisher.publishes:
            document += (
                "    tools:\n      - {ref: push-atelier-commit, "
                f"revision: {grant.revision_hash.value}}}\n"
            ).encode()
        document += declared_output()
    return document


def _graph(name: str, publishers: tuple[Publisher, ...]) -> WorkflowGraphV3:
    graph = parse_workflow_document(
        _document(name, publishers, _push_grant(_push_operation()))
    )
    assert isinstance(graph, WorkflowGraphV3), graph
    return graph


@contextmanager
def publishing_run(
    root: Path, name: str, publishers: tuple[Publisher, ...]
) -> Iterator[PublishingRun]:
    """A started run of this line, over a project whose one commit is on its remote."""
    project = root / "project"
    git_project(project, {"kept.txt": "what trunk carried first\n"})
    remote = root / "remote.git"
    run_git(root, "init", "--bare", "--quiet", str(remote))
    # The transport fetches its declared base by object name, and a base that
    # trunk has since moved past is no longer a tip of this remote.
    run_git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    run_git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    candidates = root / "candidates.git"
    run_git(root, "init", "--bare", "--quiet", str(candidates))

    operation = _push_operation()
    grant = _push_grant(operation)
    revision = WorkflowRevision(_document(name, publishers, grant))
    runtime = recording_exact_runtime(
        canonical_runtime_settings(root, "publication-pin", agent_scratch_root(root)),
        canonical_loopback_effects(root),
        b'"published"',
    )
    runtime.initialize_storage()
    try:
        publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, operation, grant)
        start_published_v3_run(
            runtime.engine,
            runtime.settings,
            RUN,
            revision,
            runtime.agent_executor_registry,
        )
        yield PublishingRun(
            runtime,
            revision,
            project,
            remote,
            candidates,
            GitTransportEffectAdapterFactory(
                candidates,
                GitRemote("local-test", str(remote), None),
                AdapterRevision("git-push-v1"),
                EffectDestination("git"),
                FakeHeadBranchPullRequests(),
            ),
        )
    finally:
        runtime.close()


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
    graph = _graph("Two publishers beside each other", (SEED, LEFT, RIGHT, JOINING))
    branch = HeadBranch("atelier2/work-item/" + "b2" * 32)
    beside_each_other = {
        publisher.node_id: RunPublication(branch, "a1" * 20, "c3" * 20)
        for publisher in (LEFT, RIGHT)
    }

    with pytest.raises(RunPublicationRefused, match="left, right"):
        last_in_workflow_order(graph, beside_each_other)
