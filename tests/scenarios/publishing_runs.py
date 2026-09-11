"""A started run whose publishers really push, over a real project and remote.

Every claim about where a run's publishers stand, and about the work they hand
one another, is a claim about commits. So a scenario built here has one real
repository whose head really moves, one bare remote the real transport really
pushes to, one bare candidate store holding real trees, and one durable run
whose receipts the production readers really read.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.advancer import prepared_effect_intent
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.node_binding_codec import EncodedAgentBindingV2
from atelier2.adapters.dbos.run_publications import NodeInRun, pinned_source_for
from atelier2.adapters.dbos.run_store import commit_confirmed_effect
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
from atelier2.contracts.agent_attempts import AgentAttemptId
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
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, RunId, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.project_verification import DeclaredProject
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.projects import commit_to_project, git_project, run_git
from tests.scenarios.runs import (
    complete_v3_agent_node,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.runtime import recording_exact_runtime
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/publishers-of-one-run")
"""The one run every scenario of this module starts."""

PUBLISHED_BRANCH = HeadBranch("atelier2/work-item/" + "b2" * 32)
"""The one lane branch every publisher of these scenarios pushes to."""

NODE_INSTRUCTION = "Carry this run one step further."
"""What every node of these lines is asked, and therefore what its job is."""


@dataclass(frozen=True, slots=True)
class Publisher:
    """One node of the scenario's line: what orders it, and what it publishes.

    `starts_from` is the node whose publication this one goes on working in,
    written into the document exactly as an author writes it.
    """

    node_id: str
    depends_on: tuple[str, ...] = ()
    publishes: bool = True
    starts_from: str | None = None

    def attempt_id(self) -> str:
        """The attempt id this node's candidate is kept under, distinct per node."""
        return self.node_id.encode().hex().ljust(64, "0")[:64]


BUILD = Publisher("build")
FIX = Publisher("fix", depends_on=("build",), starts_from="build")
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

    def keep(self, publisher: Publisher, files: Mapping[str, str]) -> CandidateTree:
        """Keep a candidate: these files over what the checkout already carries.

        Written over rather than beside, so calling this twice keeps the second
        attempt continuing the first one's work exactly as an inherited
        candidate does. Anchored under this node's own attempt id, where the
        store anchors what a real attempt captured.
        """
        for name, body in files.items():
            self.project.joinpath(name).write_text(body, encoding="utf-8")
        run_git(self.project, "add", "--all")
        tree = run_git(self.project, "write-tree")
        holder = run_git(self.project, "commit-tree", tree, "-m", "kept")
        run_git(self.candidates, "fetch", "--quiet", str(self.project), holder)
        kept = CandidateTree(AgentAttemptId(publisher.attempt_id()), tree)
        run_git(
            self.candidates,
            "update-ref",
            f"refs/atelier/candidates/{kept.attempt_id.value}",
            kept.tree,
        )
        return kept

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

    def publish(self, publisher: Publisher, candidate: CandidateTree, base: str) -> str:
        """Push this node's kept candidate for real, and confirm the receipt it earns."""
        intent = self._push_intent(publisher, candidate, base)
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

    def advance_past(self, publisher: Publisher) -> None:
        """Carry this node to its end, so the run stands on the one after it.

        The binding step refuses any node but the one a run stands on, so a
        scenario asking what a later node binds moves the run there the way a
        finished node moves it: the attempt completes, and a publisher's own
        confirmed push is what then releases the successor.
        """
        complete_v3_agent_node(
            self.runtime,
            RUN,
            publisher.node_id,
            NODE_INSTRUCTION.encode(),
            b'"carried"',
        )
        if not publisher.publishes:
            return
        with canonical_write_transaction(self.runtime.engine) as connection:
            commit_confirmed_effect(
                connection,
                logical_effect_key_for_node(
                    RUN,
                    self.revision.revision_hash,
                    publisher.node_id,
                    FIRST_ROUND_ORDINAL,
                ),
                self.revision.revision_hash,
            )

    def binding_of(self, publisher: Publisher) -> EncodedAgentBindingV2:
        """What the run's own binding step records for the node it stands on."""
        encoded = _node_binding(
            self.runtime.datasource,
            RUN,
            self.revision.revision_hash,
            publisher.node_id,
            self.declared_project(),
        )
        return cast(EncodedAgentBindingV2, encoded)

    def declared_project(self) -> DeclaredProject:
        """The project this run's attempts work in, keep in, and are verified by."""
        return declared_project(self.project, self.runtime.settings.database_path)

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

    def _push_intent(
        self, publisher: Publisher, candidate: CandidateTree, base: str
    ) -> EffectIntent:
        request = PushAtelierCommit(
            candidate.attempt_id.value,
            candidate.tree,
            base,
            PUBLISHED_BRANCH,
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


def push_operation() -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        (
            '{"author":{"email":"agent@example.test","name":"Atelier Agent"},'
            '"committer":{"email":"core@example.test","name":"Atelier Core"},'
            f'"operation":"{AdapterOperationName.PUSH_ATELIER_COMMIT.value}"}}'
        ).encode(),
    )


def push_grant(operation: PublishedRevision) -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.TOOL,
        (
            f'{{"capability":"{ToolGrantCapability.PUSH_ATELIER_COMMIT.value}",'
            f'"operation":{{"ref":"push-atelier-commit",'
            f'"revision":"{operation.revision_hash.value}"}}}}'
        ).encode(),
    )


def workflow_document(
    name: str, publishers: tuple[Publisher, ...], grant: PublishedRevision
) -> bytes:
    """The document of this line, authored the way an author would write it."""
    document = f"format_version: 3\nname: {name}\nnodes:\n".encode()
    for publisher in publishers:
        document += f"""  - id: {publisher.node_id}
    type: agent
    role: builder
    mode: headless
    instruction: {NODE_INSTRUCTION}
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
        if publisher.starts_from is not None:
            document += f"    starts_from: {{node: {publisher.starts_from}}}\n".encode()
        document += declared_output()
    return document


def workflow_graph(name: str, publishers: tuple[Publisher, ...]) -> WorkflowGraphV3:
    """The parsed graph of this line, without a run or a store behind it."""
    graph = parse_workflow_document(
        workflow_document(name, publishers, push_grant(push_operation()))
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
    candidates = root / CANDIDATE_STORE_DIRECTORY_NAME
    run_git(root, "init", "--bare", "--quiet", str(candidates))

    operation = push_operation()
    grant = push_grant(operation)
    revision = WorkflowRevision(workflow_document(name, publishers, grant))
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
