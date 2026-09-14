"""A V3 Agent→Action line lands one pull request through the GitHub adapter."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.advancer import (
    graph_action_intent,
    prepare_graph_action,
    prepared_effect_intent,
)
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    encode_found,
    intent_snapshot_from_record,
)
from atelier2.adapters.dbos.run_publications import RunPublicationRefused
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    run_events,
    run_fork_effect_fences,
    run_forks,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.github.effects import (
    GitHubEffectAdapterFactory,
    RecordedPullRequest,
)
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    AuthoredOrder,
    RunCreated,
    start_published_run,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effect_markers import body_carries_request_hash
from atelier2.contracts.effect_requests import (
    GitCommitIdentity,
    HeadBranch,
    PushAtelierCommit,
    PushAtelierCommitReceipt,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAdapterBinding,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectReadback,
    EffectResult,
    EffectUnknownOutcome,
    PerformedEffect,
    ReadbackPhase,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
    logical_effect_key_for_node,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.orders import ObservedWorkItemOrderValue
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_forks import RunForkCommandId, successor_run_id_for
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT,
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import AgentProcessCommand
from atelier2.ports.durable_run_forks import (
    DurableRunForkCreated,
    DurableRunForkStateCorrupt,
    ForkRunRequest,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.effects import (
    EffectAdapter,
    EffectAdapterRegistration,
    EffectAdapterRegistry,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    AgentCommandFactory,
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    answering_each_execution,
    launching,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.projects import git_project, run_git
from tests.scenarios.runs import (
    publish_pinned_revisions,
    publish_revision,
    submit_wait_answer,
)
from tests.scenarios.work_item_claims import fake_agent_claim_executable
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/open-pr")
TRANSITIVE_RUN = RunId("v3/open-pr/transitive")
TWO_PUBLISHER_RUN = RunId("v3/open-pr/two-publishers")
BOUND_PROJECT = ProjectId("project-with-two-publishers")
BUILDING_NODE = "build"
FIXING_NODE = "fix"
ACTION_NODE = "publish"
ORDERED_ITEM = ObservedWorkItemRevision(
    TrackerItemReference("gh:1528"),
    WorkItemKind.ISSUE,
    b"Open the pull request over the last publication of the run.\n"
    b"\n## Bereich\nkept.txt\n",
    WorkItemChangeMarker("issue-1528-v1"),
    RecordedAt("2026-09-11T09:00:00Z"),
)
BUILD_REPORT = b'"what the builder published"'
FIX_REPORT = b'"what the fixer published after the builder"'
"""Two reports of distinct bytes, so an opened pull request names which it read."""
TREE = json.dumps({"files": {"hello.txt": "from the builder"}}).encode("utf-8")
REVIEWERS_VERDICT = json.dumps(
    {"verdict": "approved by the reviewer, not the builder"}
).encode("utf-8")
"""The reviewer's own output -- distinct bytes from `TREE`, the builder's own.

The Action's `body` input must be traced to `implement`'s output specifically,
not to whichever agent happened to run last (#1101): identical output would
let a wrong dependency-closure read pass unnoticed.
"""
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"
OPEN_PR_DOCUMENT = json.dumps({"operation": AdapterOperationName.OPEN_PR.value}).encode(
    "utf-8"
)
_LIST_THEN_WRITE = (
    "import os, pathlib, sys; "
    "pathlib.Path(sys.argv[1]).write_text('\\n'.join(sorted(os.listdir('.')))); "
    "os.write(1, bytes.fromhex(sys.argv[2]))"
)
_WRITE_THEN_ANSWER = (
    "import os, pathlib, sys; "
    "pathlib.Path(sys.argv[1]).write_text(sys.argv[1]); "
    "os.write(1, bytes.fromhex(sys.argv[2]))"
)
_STANDING_PATIENCE_SECONDS = 30.0
"""How long two real agent attempts and two real pushes may take to stand aside."""
_STANDING_POLL_SECONDS = 0.025
_PUSHED = AdapterOperationName.PUSH_ATELIER_COMMIT
_PUSH_AUTHOR = GitCommitIdentity("Atelier Agent", "agent@example.test")
_PUSH_COMMITTER = GitCommitIdentity("Atelier Core", "core@example.test")
PUSH_OPERATION = PublishedRevision(
    RevisionKind.ADAPTER_OPERATION,
    json.dumps(
        {
            "operation": _PUSHED.value,
            "author": _PUSH_AUTHOR.as_json(),
            "committer": _PUSH_COMMITTER.as_json(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
)
PUSH_GRANT = PublishedRevision(
    RevisionKind.TOOL,
    json.dumps(
        {
            "capability": ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
            "operation": {
                "ref": "push-atelier-commit",
                "revision": PUSH_OPERATION.revision_hash.value,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
)


class CountingGitHubEffectAdapter:
    def __init__(
        self, owner: CountingGitHubEffectAdapterFactory, delegate: EffectAdapter
    ) -> None:
        self._owner = owner
        self._delegate = delegate

    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        self._owner.readback_calls += 1
        return self._delegate.readback(intent, phase)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        self._owner.execute_calls += 1
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class CountingGitHubEffectAdapterFactory:
    def __init__(self, database: Path) -> None:
        self._delegate = GitHubEffectAdapterFactory(
            database,
            AdapterRevision("github-open-pr-v1"),
            EffectDestination("platform"),
        )
        self.readback_calls = 0
        self.execute_calls = 0

    @property
    def database_path(self) -> Path:
        return self._delegate.database_path

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

    def open(self) -> CountingGitHubEffectAdapter:
        return CountingGitHubEffectAdapter(self, self._delegate.open())

    def recorded_pull_requests(self) -> tuple[RecordedPullRequest, ...]:
        return self._delegate.recorded_pull_requests()


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path]]:
    started, github, listing = _runtime_for(tmp_path, tmp_path / "github.sqlite")
    started.initialize_storage()
    try:
        yield started, github, listing, tmp_path / "atelier.sqlite"
    finally:
        started.close()


def _runtime_for(
    root: Path, github_database: Path
) -> tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path]:
    listing = root / "lease-listing.txt"
    recording = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        TREE,
        command=launching(
            sys.executable,
            "-c",
            _LIST_THEN_WRITE,
            str(listing),
            TREE.hex(),
        ),
    )
    github = CountingGitHubEffectAdapterFactory(github_database)
    started = DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v3-open-pr-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        github,
        (recording,),
    )
    return started, github, listing


def _transitive_line_runtime_for(
    root: Path, github_database: Path
) -> tuple[DbosRuntime, CountingGitHubEffectAdapterFactory]:
    """`_runtime_for`'s twin for the builder-then-reviewer line: the two agents
    answer with distinct bytes, so a body traced to the wrong one is caught.
    """
    recording = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        b"",
        command=answering_each_execution(
            {
                ("implement", FIRST_ROUND_ORDINAL): TREE,
                ("review", FIRST_ROUND_ORDINAL): REVIEWERS_VERDICT,
            }
        ),
    )
    github = CountingGitHubEffectAdapterFactory(github_database)
    started = DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v3-open-pr-transitive-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        github,
        (recording,),
    )
    return started, github


def publish_line(
    runtime: DbosRuntime, *, action_successor: bool = False
) -> tuple[WorkflowRevision, AgentBindingSet]:
    catalog_store = DbosCatalogStore(runtime.engine)
    for revision in (
        ANY_JSON_SCHEMA,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT),
    ):
        published = catalog_store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published
    operation_hash = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT
    ).revision_hash.value
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    document = (
        b"""format_version: 3
name: Land the tree
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write the tree this chain lands.
"""
        + declared_output()
        + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: {operation_hash}}}
    depends_on: [implement]
    inputs:
      - name: body
        from: {{node: implement, output: result}}
""".encode()
        + (
            b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Review the confirmed publication.
    depends_on: [publish]
"""
            + declared_output()
            if action_successor
            else b""
        )
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def publish_transitive_line(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """A builder's output opens a pull request behind review then a Wait.

    `_action_predecessor` is retired: the Action's `body` input names
    `implement` by its own output, not `approve`'s immediate `depends_on`
    edge, and `graph_action_intent` still reads it through the dependency
    closure the review and the Wait stand inside of (#1101).
    """
    catalog_store = DbosCatalogStore(runtime.engine)
    for revision in (
        ANY_JSON_SCHEMA,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT),
    ):
        published = catalog_store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published
    operation_hash = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT
    ).revision_hash.value
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    document = (
        b"""format_version: 3
name: A builder's output opens a pull request behind review and a wait
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write the tree this chain lands.
"""
        + declared_output()
        + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the tree the builder wrote.
    depends_on: [implement]
"""
        + declared_output()
        + b"""  - id: approve
    type: wait
    prompt: Release the reviewed candidate?
    depends_on: [review]
"""
        + declared_output(ANY_JSON_SCHEMA, "approval")
        + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: {operation_hash}}}
    depends_on: [approve]
    inputs:
      - name: body
        from: {{node: implement, output: result}}
""".encode()
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (
            AgentBinding(AgentRole("builder"), configuration.revision_hash),
            AgentBinding(AgentRole("reviewer"), configuration.revision_hash),
        )
    )


def _two_publisher_document(body_from: str) -> bytes:
    """`build`, a `fix` ordered behind it, and the Action reading one report.

    Both agents hold the push grant, so each publishes a candidate of its own
    onto the branch this run's work item derives. The Action's `body` names
    which of the two reports the pull request would carry.
    """
    open_pr_hash = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT
    ).revision_hash.value
    return (
        f"""format_version: 3
name: A fix publishes behind the build, then one pull request opens
graph_inputs:
  - name: work_item
    schema:
      ref: work-item
      revision: {WORK_ITEM_ORDER_SCHEMA_REVISION.value}
nodes:
""".encode()
        + _publishing_node(BUILDING_NODE, ())
        + _publishing_node(FIXING_NODE, (BUILDING_NODE,))
        + f"""  - id: {ACTION_NODE}
    type: action
    operation: {{ref: open-pr, revision: {open_pr_hash}}}
    depends_on: [{FIXING_NODE}]
    inputs:
      - name: body
        from: {{node: {body_from}, output: result}}
""".encode()
    )


def _publishing_node(node_id: str, depends_on: tuple[str, ...]) -> bytes:
    ordered = f"    depends_on: [{', '.join(depends_on)}]\n" if depends_on else ""
    ordered_item = (
        ""
        if depends_on
        else "    inputs:\n      - name: work_item\n"
        "        from: {graph_input: work_item}\n"
    )
    return (
        f"""  - id: {node_id}
    type: agent
    role: builder
    mode: headless_with_tools
    instruction: Carry this run one step further and leave your candidate behind.
    tools:
      - {{ref: push-atelier-commit, revision: {PUSH_GRANT.revision_hash.value}}}
{ordered}{ordered_item}""".encode()
        + declared_output()
    )


def _publishing_reports(reports: Mapping[str, bytes]) -> AgentCommandFactory:
    """Each publisher changes a file of its own and answers with its own report.

    The candidate the atelier captures differs per node, so the two
    publications of one run are two different commits rather than one tree
    pushed twice.
    """

    def command(request: AgentExecutionRequestV2) -> AgentProcessCommand:
        return launching(
            sys.executable,
            "-c",
            _WRITE_THEN_ANSWER,
            f"{request.node_id}.txt",
            reports[request.node_id].hex(),
        )(request)

    return command


def _tooled_builder_binding(runtime: DbosRuntime) -> tuple[AuthoredAgentBinding, ...]:
    """The role both publishers bind: a workspace agent whose grant may push."""
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    return (AuthoredAgentBinding("builder", configuration.revision_hash.value),)


@dataclass(frozen=True, slots=True)
class TwoPublisherRun:
    """A launched run of that line, and what its publishers really left behind."""

    runtime: DbosRuntime
    github: CountingGitHubEffectAdapterFactory
    workflow: WorkflowRevision
    remote: Path

    def publication_of(self, publisher: str) -> PushAtelierCommitReceipt:
        """What this node's confirmed push receipt says it put on the branch."""
        with self.runtime.engine.connect() as connection:
            result = connection.execute(
                sa.select(effect_receipts.c.result).where(
                    effect_receipts.c.logical_key
                    == logical_effect_key_for_node(
                        TWO_PUBLISHER_RUN,
                        self.workflow.revision_hash,
                        publisher,
                        FIRST_ROUND_ORDINAL,
                    ).value
                )
            ).scalar_one()
        return PushAtelierCommitReceipt.from_result_bytes(bytes(result))

    def remote_head_of(self, full_ref: str) -> str:
        """Which commit the destination itself now answers for that ref."""
        return run_git(self.remote, "rev-parse", full_ref)

    def effects_its_action_asked_for(self) -> tuple[AdapterOperationName, ...]:
        """Every effect intent the Action's own node execution durably holds."""
        with self.runtime.engine.connect() as connection:
            return tuple(
                AdapterOperationName(str(operation))
                for operation in connection.scalars(
                    sa.select(effect_intents.c.operation_name).where(
                        effect_intents.c.logical_key
                        == logical_effect_key_for_node(
                            TWO_PUBLISHER_RUN,
                            self.workflow.revision_hash,
                            ACTION_NODE,
                            FIRST_ROUND_ORDINAL,
                        ).value
                    )
                )
            )

    def wait_until_standing_at_its_action(self) -> None:
        """Wait until both publishers confirmed and the Action is the next node.

        An Action whose report was overtaken never gets past its refusal, so a
        scenario about that refusal waits for the node the run stands on, not
        for a state it reaches afterwards.
        """
        self._wait_until(
            "its Action",
            lambda: self._standing() == (ACTION_NODE, RunState.STARTED),
        )

    def wait_until_it_completes(self) -> None:
        """Wait until the run ended, its publications and pull request included."""
        self._wait_until("its end", lambda: self._standing()[1] is RunState.COMPLETED)

    def _standing(self) -> tuple[str, RunState]:
        with self.runtime.engine.connect() as connection:
            record = connection.execute(
                sa.select(runs.c.current_node_id, runs.c.state).where(
                    runs.c.run_id == TWO_PUBLISHER_RUN.value
                )
            ).one()
        return str(record.current_node_id), RunState(str(record.state))

    def _wait_until(self, awaited: str, reached: Callable[[], bool]) -> None:
        deadline = time.monotonic() + _STANDING_PATIENCE_SECONDS
        while time.monotonic() < deadline:
            if reached():
                return
            time.sleep(_STANDING_POLL_SECONDS)
        raise AssertionError(f"the run stands at {self._standing()}, not at {awaited}")


@contextmanager
def two_publisher_run(root: Path, body_from: str) -> Iterator[TwoPublisherRun]:
    """A launched run of that line over a real project, remote and candidate store.

    Nothing about the publications is seeded: both agents run, the transport
    really pushes, and the branch the Action would open over really carries
    what the second push put there.
    """
    project = root / "project"
    git_project(project, {"kept.txt": "what trunk carried first\n"})
    remote = root / "remote.git"
    run_git(root, "init", "--bare", "--quiet", str(remote))
    run_git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    github = CountingGitHubEffectAdapterFactory(root / "github.sqlite")
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v3-open-pr-two-publishers",
            agent_scratch_root=agent_scratch_root(root),
            project_id=BOUND_PROJECT,
            bootstrap_project_root=project,
            aco_executable=fake_agent_claim_executable(root),
        ),
        EffectAdapterRegistry(
            (
                EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
                EffectAdapterRegistration(
                    AdapterOperationName.PUSH_ATELIER_COMMIT,
                    GitTransportEffectAdapterFactory(
                        root / CANDIDATE_STORE_DIRECTORY_NAME,
                        GitRemote("local-test", str(remote)),
                        AdapterRevision("git-push-v1"),
                        EffectDestination("git"),
                        FakeHeadBranchPullRequests(),
                    ),
                ),
            )
        ),
        (
            RecordingAgentExecutorFactoryV2(
                "exact",
                "exact/v1",
                "exact-operation",
                b"",
                capability_set=frozenset(
                    {AgentExecutionCapability.HEADLESS_WITH_TOOLS}
                ),
                command=_publishing_reports(
                    {BUILDING_NODE: BUILD_REPORT, FIXING_NODE: FIX_REPORT}
                ),
            ),
        ),
    )
    runtime.initialize_storage()
    try:
        publish_pinned_revisions(
            runtime.engine,
            ANY_JSON_SCHEMA,
            PublishedRevision(RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT),
            PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
            PUSH_OPERATION,
            PUSH_GRANT,
        )
        workflow = WorkflowRevision(_two_publisher_document(body_from))
        publish_revision(runtime.engine, workflow)
        started = start_published_run(
            TWO_PUBLISHER_RUN,
            workflow.revision_hash,
            _tooled_builder_binding(runtime),
            DbosDurableRunStarter(
                runtime.engine, runtime.settings, runtime.agent_executor_registry
            ),
            orders=(
                AuthoredOrder("work_item", ObservedWorkItemOrderValue(ORDERED_ITEM)),
            ),
        )
        assert isinstance(started, RunCreated), started
        runtime.launch()
        yield TwoPublisherRun(runtime, github, workflow, remote)
    finally:
        runtime.close()


def wait_for_state(runtime: DbosRuntime, state: RunState, run_id: RunId = RUN) -> None:
    deadline = time.monotonic() + 8
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def _complete_origin(runtime: DbosRuntime) -> None:
    workflow, bindings = publish_line(runtime)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)
    runtime.launch()
    wait_for_state(runtime, RunState.COMPLETED)


def test_forked_action_references_the_confirmed_pull_request_without_replaying_it(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    started = starter.start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)

    request = ForkRunRequest(RUN, "retry-publish", "publish")
    forked = starter.fork_run(request)
    assert isinstance(forked, DurableRunForkCreated)
    successor = successor_run_id_for(RunForkCommandId.for_request(RUN, "retry-publish"))
    wait_for_state(started_runtime, RunState.COMPLETED, successor)

    assert len(github.recorded_pull_requests()) == 1
    assert calls_before_fork == (github.readback_calls, github.execute_calls)
    with started_runtime.engine.connect() as connection:
        successor_receipt = (
            connection.execute(
                sa.select(effect_receipts).where(
                    effect_receipts.c.run_id == successor.value
                )
            )
            .mappings()
            .one()
        )
        successor_events = tuple(
            connection.execute(
                sa.select(run_events.c.event_kind).where(
                    run_events.c.run_id == successor.value
                )
            ).scalars()
        )
    assert successor_events == (RunEventKind.ACTION_COMPLETED.value,)
    assert successor_receipt["confirmation_source"] == "FORK_REFERENCE"
    assert successor_receipt["fork_source_run_id"] == RUN.value
    assert successor_receipt["fork_source_logical_key"] is not None
    assert successor_receipt["fork_source_result_hash"] is not None


def test_fork_reuses_a_successfully_confirmed_action_before_the_target(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime, action_successor=True)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)

    forked = starter.fork_run(ForkRunRequest(RUN, "reuse-action", "review"))

    assert isinstance(forked, DurableRunForkCreated)
    assert tuple(entry.node_id for entry in forked.fork.reused_nodes) == (
        "implement",
        "publish",
    )
    wait_for_state(started_runtime, RunState.COMPLETED, forked.run.run_id)
    assert calls_before_fork == (github.readback_calls, github.execute_calls)
    assert len(github.recorded_pull_requests()) == 1


def test_action_fork_with_changed_request_waits_without_invoking_the_adapter(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)
    factory = next(
        entry.factory
        for entry in started_runtime.agent_executor_registry.entries
        if isinstance(entry.factory, RecordingAgentExecutorFactoryV2)
    )
    assert isinstance(factory, RecordingAgentExecutorFactoryV2)
    assert factory.opened is not None
    changed_tree = json.dumps({"files": {"hello.txt": "changed by the fork"}}).encode()
    factory.opened.command = launching(
        sys.executable,
        "-c",
        _LIST_THEN_WRITE,
        str(_listing),
        changed_tree.hex(),
    )

    forked = starter.fork_run(ForkRunRequest(RUN, "changed-action", "implement"))
    assert isinstance(forked, DurableRunForkCreated)
    wait_for_state(started_runtime, RunState.WAITING_RECONCILIATION, forked.run.run_id)

    assert calls_before_fork == (github.readback_calls, github.execute_calls)
    assert len(github.recorded_pull_requests()) == 1


def test_fork_with_a_missing_effect_fence_waits_without_invoking_the_adapter(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, _github, _listing, atelier_database = runtime
    _complete_origin(started_runtime)
    started_runtime.close()
    restarted, github, _listing = _runtime_for(
        atelier_database.parent, atelier_database.parent / "github.sqlite"
    )
    try:
        restarted.initialize_storage()
        starter = DbosDurableRunStarter(
            restarted.engine,
            restarted.settings,
            restarted.agent_executor_registry,
        )
        forked = starter.fork_run(
            ForkRunRequest(RUN, "missing-effect-fence", "publish")
        )
        assert isinstance(forked, DurableRunForkCreated)
        with restarted.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER run_fork_effect_fences_no_delete")
            connection.execute(
                run_fork_effect_fences.delete().where(
                    run_fork_effect_fences.c.successor_run_id == forked.run.run_id.value
                )
            )

        restarted.launch()
        wait_for_state(restarted, RunState.WAITING_RECONCILIATION, forked.run.run_id)

        assert github.readback_calls == github.execute_calls == 0
        assert len(github.recorded_pull_requests()) == 1
        with restarted.engine.connect() as connection:
            assert connection.execute(
                sa.select(runs.c.state, effect_intents.c.state)
                .join(effect_intents, effect_intents.c.run_id == runs.c.run_id)
                .where(runs.c.run_id == forked.run.run_id.value)
            ).one() == (
                RunState.WAITING_RECONCILIATION.value,
                "WAITING_RECONCILIATION",
            )
            assert tuple(
                connection.scalars(
                    sa.select(run_events.c.event_kind).where(
                        run_events.c.run_id == forked.run.run_id.value
                    )
                )
            ) == (RunEventKind.ACTION_RECONCILIATION_REQUIRED.value,)
    finally:
        restarted.close()


def test_fork_adapter_identity_mismatch_commits_waiting_without_adapter_calls(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, _github, _listing, atelier_database = runtime
    _complete_origin(started_runtime)
    started_runtime.close()
    restarted, github, _listing = _runtime_for(
        atelier_database.parent,
        atelier_database.parent / "github.sqlite",
    )
    try:
        restarted.initialize_storage()
        starter = DbosDurableRunStarter(
            restarted.engine,
            restarted.settings,
            restarted.agent_executor_registry,
        )
        forked = starter.fork_run(
            ForkRunRequest(RUN, "adapter-identity-mismatch", "publish")
        )
        assert isinstance(forked, DurableRunForkCreated)
        with restarted.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER effect_receipts_no_update")
            connection.execute(
                effect_receipts.update()
                .where(effect_receipts.c.run_id == RUN.value)
                .values(adapter_operational_identity="mismatched-source-operation")
            )

        restarted.launch()
        wait_for_state(restarted, RunState.WAITING_RECONCILIATION, forked.run.run_id)

        assert github.readback_calls == github.execute_calls == 0
        assert len(github.recorded_pull_requests()) == 1
        with restarted.engine.connect() as connection:
            assert connection.execute(
                sa.select(runs.c.state, effect_intents.c.state)
                .join(effect_intents, effect_intents.c.run_id == runs.c.run_id)
                .where(runs.c.run_id == forked.run.run_id.value)
            ).one() == (
                RunState.WAITING_RECONCILIATION.value,
                "WAITING_RECONCILIATION",
            )
            assert tuple(
                connection.scalars(
                    sa.select(run_events.c.event_kind).where(
                        run_events.c.run_id == forked.run.run_id.value
                    )
                )
            ) == (RunEventKind.ACTION_RECONCILIATION_REQUIRED.value,)
    finally:
        restarted.close()


def test_missing_confirmed_action_receipt_refuses_the_fork_before_any_side_effect(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)
    with started_runtime.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("DROP TRIGGER effect_receipts_no_delete")
        connection.execute(
            effect_receipts.delete().where(effect_receipts.c.run_id == RUN.value)
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    refused = starter.fork_run(ForkRunRequest(RUN, "missing-action-receipt", "publish"))

    assert isinstance(refused, DurableRunForkStateCorrupt)
    assert calls_before_fork == (github.readback_calls, github.execute_calls)
    with started_runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(run_forks)) == 0


def test_unreached_action_without_a_receipt_does_not_block_a_full_fork(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    factory = next(
        entry.factory
        for entry in started_runtime.agent_executor_registry.entries
        if isinstance(entry.factory, RecordingAgentExecutorFactoryV2)
    )
    assert isinstance(factory, RecordingAgentExecutorFactoryV2)
    assert factory.opened is not None
    factory.opened.command = launching(
        sys.executable,
        "-c",
        _LIST_THEN_WRITE,
        str(_listing),
        b"not-json".hex(),
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.FAILED)
    factory.opened.command = launching(
        sys.executable,
        "-c",
        _LIST_THEN_WRITE,
        str(_listing),
        TREE.hex(),
    )

    forked = starter.fork_run(ForkRunRequest(RUN, "unreached-action", "implement"))

    assert isinstance(forked, DurableRunForkCreated)
    wait_for_state(started_runtime, RunState.COMPLETED, forked.run.run_id)
    assert github.readback_calls == 1
    assert github.execute_calls == 1
    assert len(github.recorded_pull_requests()) == 1


@dataclass(frozen=True, slots=True)
class _PushReceiptMutation:
    operation: AdapterOperationName = AdapterOperationName.PUSH_ATELIER_COMMIT
    branch: HeadBranch | None = None
    full_ref: str | None = None
    commit_oid: str | None = None
    effect_id: str | None = None
    remote_identity: str = "remote"
    author: GitCommitIdentity | None = None
    committer: GitCommitIdentity | None = None
    candidate_tree: str | None = None
    parent: str | None = None


def _push_intent(
    workflow: WorkflowRevision,
    mutation: _PushReceiptMutation | None = None,
) -> tuple[EffectIntent, PushAtelierCommit]:
    mutation = mutation or _PushReceiptMutation()
    request = PushAtelierCommit(
        "a1" * 32,
        "b2" * 20,
        "c3" * 20,
        HeadBranch("atelier2/work-item/confirmed-push"),
        GitCommitIdentity("Atelier Agent", "agent@example.test"),
        GitCommitIdentity("Atelier Core", "core@example.test"),
        "2026-08-27T12:34:56Z",
    )
    return (
        EffectIntent(
            EffectBinding(
                logical_effect_key_for_node(
                    RUN, workflow.revision_hash, "implement", 1
                ),
                RUN,
                workflow.revision_hash,
                AdapterRevision("git-push-v1"),
                EffectDestination("git"),
                AdapterOperationalIdentity("remote"),
                mutation.operation,
            ),
            CanonicalRequest(request.canonical_bytes()),
        ),
        request,
    )


def _confirm_push_receipt(
    connection: Any,
    workflow: WorkflowRevision,
    mutation: _PushReceiptMutation,
) -> None:
    intent, request = _push_intent(workflow, mutation)
    prepared_effect_intent(connection, intent)
    expected = request.expected_commit_oid(intent.request.request_hash.value)
    branch = mutation.branch or request.head_branch
    commit_oid = mutation.commit_oid or expected
    performed = PerformedEffect(
        EffectId(mutation.effect_id or commit_oid),
        EffectResult(
            PushAtelierCommitReceipt(
                mutation.remote_identity,
                mutation.full_ref or branch.full_ref,
                commit_oid,
                mutation.parent or request.base_commit,
                mutation.candidate_tree or request.candidate_tree,
                branch.value,
                mutation.author or request.author,
                mutation.committer or request.committer,
            ).result_bytes()
        ),
    )
    assert (
        commit_resolution(
            connection,
            intent.binding.logical_key.value,
            workflow.revision_hash.value,
            encode_found(performed, ConfirmationSource.ADAPTER_EXECUTION),
        )
        is RunState.STARTED
    )


@pytest.mark.parametrize(
    "unconfirmed_push",
    [
        pytest.param(False, id="absent-push-intent"),
        pytest.param(True, id="unconfirmed-push-intent"),
    ],
)
def test_a_project_open_pr_action_refuses_without_a_confirmed_push_receipt(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
    unconfirmed_push: bool,
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_retry = (github.readback_calls, github.execute_calls)
    with started_runtime.engine.begin() as connection:
        if unconfirmed_push:
            push_intent, _request = _push_intent(workflow)
            prepared_effect_intent(
                connection,
                push_intent,
            )
        connection.execute(
            runs.update()
            .where(runs.c.run_id == RUN.value)
            .values(state=RunState.STARTED.value, terminal_hash=None)
        )
        with pytest.raises(RunPublicationRefused, match="confirmed push receipt"):
            graph_action_intent(
                connection,
                RUN,
                workflow.revision_hash,
                github.binding,
                ProjectId("project-without-a-push"),
            )

    assert calls_before_retry == (github.readback_calls, github.execute_calls)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            _PushReceiptMutation(operation=AdapterOperationName.OPEN_PR),
            id="wrong-operation-binding",
        ),
        pytest.param(
            _PushReceiptMutation(
                branch=HeadBranch("atelier2/work-item/other"),
                full_ref="refs/heads/atelier2/work-item/other",
            ),
            id="branch-and-full-ref",
        ),
        pytest.param(
            _PushReceiptMutation(commit_oid="d4" * 20, effect_id="d4" * 20),
            id="commit-effect-and-deterministic-identity",
        ),
        pytest.param(
            _PushReceiptMutation(effect_id="f6" * 20),
            id="result-commit-and-effect-identity",
        ),
        pytest.param(
            _PushReceiptMutation(remote_identity="other-remote"),
            id="remote-identity",
        ),
        pytest.param(
            _PushReceiptMutation(
                author=GitCommitIdentity("Another Agent", "other@example.test"),
                committer=GitCommitIdentity("Another Core", "other-core@example.test"),
                candidate_tree="d4" * 20,
                parent="e5" * 20,
            ),
            id="author-tree-and-base",
        ),
    ],
)
def test_a_project_open_pr_action_refuses_a_corrupt_confirmed_push_receipt(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
    mutation: _PushReceiptMutation,
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_retry = (github.readback_calls, github.execute_calls)
    with started_runtime.engine.begin() as connection:
        _confirm_push_receipt(connection, workflow, mutation)
        connection.execute(
            runs.update()
            .where(runs.c.run_id == RUN.value)
            .values(state=RunState.STARTED.value, terminal_hash=None)
        )
        with pytest.raises(RunPublicationRefused, match="confirmed push receipt"):
            graph_action_intent(
                connection,
                RUN,
                workflow.revision_hash,
                github.binding,
                ProjectId("project-with-a-corrupt-push"),
            )

    assert calls_before_retry == (github.readback_calls, github.execute_calls)


def test_a_project_open_pr_action_refuses_the_report_a_later_publisher_overtook(
    tmp_path: Path,
) -> None:
    """`fix` published after `build`, so the branch no longer shows `build`'s work.

    A pull request over the overtaken report would describe something other
    than the head it shows, so the refusal names both nodes -- and it stands
    before anything is asked for: no open-pr intent is written and the platform
    is never called.
    """
    with two_publisher_run(tmp_path, BUILDING_NODE) as run:
        run.wait_until_standing_at_its_action()

        with (
            pytest.raises(
                RunPublicationRefused,
                match=(
                    "open-pr reads the report of `build`, but `fix` published after it"
                ),
            ),
            run.runtime.engine.begin() as connection,
        ):
            prepare_graph_action(
                connection,
                TWO_PUBLISHER_RUN,
                run.workflow.revision_hash,
                run.github.binding,
                BOUND_PROJECT,
            )

        asked_for = run.effects_its_action_asked_for()
        opened = run.github.recorded_pull_requests()
        asked = (run.github.readback_calls, run.github.execute_calls)

    assert asked_for == ()
    assert opened == ()
    assert asked == (0, 0)


def test_a_project_open_pr_action_opens_over_the_last_publication_of_its_run(
    tmp_path: Path,
) -> None:
    """The pull request the platform records stands on the run's last publication.

    Both publishers really push, so the branch carries the builder's commit and
    then the fixer's own; the Action reads the fixer's report and the recorded
    pull request is the one over the commit the destination now answers with.
    """
    with two_publisher_run(tmp_path, FIXING_NODE) as run:
        run.wait_until_it_completes()

        built = run.publication_of(BUILDING_NODE)
        fixed = run.publication_of(FIXING_NODE)
        standing_head = run.remote_head_of(fixed.full_ref)
        opened = run.github.recorded_pull_requests()
        asked_for = run.effects_its_action_asked_for()

    assert built.commit_oid != fixed.commit_oid
    assert standing_head == fixed.commit_oid
    assert [pull_request.branch for pull_request in opened] == [fixed.branch]
    assert FIX_REPORT.decode("utf-8") in opened[0].body
    assert BUILD_REPORT.decode("utf-8") not in opened[0].body
    assert asked_for == (AdapterOperationName.OPEN_PR,)


def durable_bytes_contain(database: Path, token: str) -> bool:
    needle = token.encode("utf-8")
    for candidate in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if candidate.is_file() and needle in candidate.read_bytes():
            return True
    return False


@pytest.mark.proves("a-v3-action-opens-one-pr-and-a-replay-does-not-create-a-twin")
def test_a_v3_agent_then_action_opens_one_pull_request_through_the_github_adapter(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, listing, atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)

    recorded = github.recorded_pull_requests()
    assert len(recorded) == 1
    pull_request = recorded[0]
    assert pull_request.pr_number == 1
    with started_runtime.engine.connect() as connection:
        events = [
            (
                str(record["node_id"]),
                str(record["event_kind"]),
                bytes(record["payload"]),
            )
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        intent = intent_snapshot_from_record(
            connection.execute(sa.select(effect_intents)).mappings().one()
        ).intent
        receipt_payload = bytes(
            connection.execute(sa.select(effect_receipts.c.result)).scalar_one()
        )
    assert events[0][:2] == ("implement", RunEventKind.AGENT_COMPLETED.value)
    assert events[0][2] == TREE
    assert events[1][:2] == ("publish", RunEventKind.ACTION_COMPLETED.value)
    result = json.loads(events[1][2].decode("utf-8"))
    assert result == {"branch": pull_request.branch, "pr_number": 1}
    assert body_carries_request_hash(
        pull_request.body, intent.request.request_hash.value
    )
    assert json.loads(receipt_payload.decode("utf-8")) == result

    adapter = github.open()
    try:
        replayed = adapter.execute(intent)
    finally:
        adapter.close()
    assert isinstance(replayed, PerformedEffect)
    assert json.loads(replayed.result.payload.decode("utf-8")) == result
    assert len(github.recorded_pull_requests()) == 1

    assert listing.is_file()
    names = listing.read_text().splitlines()
    assert ".git" not in names
    assert CANARY_TOKEN not in listing.read_text()
    assert CANARY_TOKEN not in pull_request.body
    assert CANARY_TOKEN.encode() not in events[1][2]
    assert CANARY_TOKEN.encode() not in receipt_payload
    assert not durable_bytes_contain(atelier_sqlite, CANARY_TOKEN)
    assert not durable_bytes_contain(github.database_path, CANARY_TOKEN)

    api = durable_api_client(started_runtime)
    public_ref = encode_public_run_reference(RUN)
    run = api.get(f"{API_PREFIX}/runs/{public_ref}")
    assert run.status_code == 200, run.text
    assert CANARY_TOKEN not in run.text
    stream = api.get(f"{API_PREFIX}/runs/{public_ref}/events")
    assert stream.status_code == 200, stream.text
    assert CANARY_TOKEN not in stream.text
    streamed = [
        json.loads(line.removeprefix("data: "))
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert streamed[-1]["event"] == "ACTION_COMPLETED"
    assert streamed[-1]["workflow_format_version"] == 3
    assert "receipt" in streamed[-1]
    node = api.get(f"{API_PREFIX}/runs/{public_ref}/nodes/implement")
    assert node.status_code == 200, node.text
    assert CANARY_TOKEN not in node.text


@pytest.mark.proves(
    "an-open-pr-action-reads-the-builders-output-through-review-and-a-wait"
)
def test_open_pr_reads_the_builders_output_through_review_and_a_wait(
    tmp_path: Path,
) -> None:
    """The retired immediate-predecessor rule is not needed: review and a Wait
    stand between the builder and the Action, and the Action still reads the
    builder's own output through the dependency closure that orders them --
    not merely a shared idempotency marker both agents would carry alike
    (#1101). `implement` and `review` answer with distinct bytes so the
    recorded body can be traced to the builder's own, and not the reviewer's.
    """
    started_runtime, github = _transitive_line_runtime_for(
        tmp_path, tmp_path / "github.sqlite"
    )
    started_runtime.initialize_storage()
    try:
        workflow, bindings = publish_transitive_line(started_runtime)
        starter = DbosDurableRunStarter(
            started_runtime.engine,
            started_runtime.settings,
            started_runtime.agent_executor_registry,
        )
        started = starter.start_published(
            StartPublishedRunRequestV2(TRANSITIVE_RUN, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        started_runtime.launch()
        wait_for_state(started_runtime, RunState.WAITING_INPUT, TRANSITIVE_RUN)

        submit_wait_answer(
            started_runtime.engine,
            started_runtime.settings.application_version,
            SubmitWaitAnswerRequest(
                TRANSITIVE_RUN,
                workflow.revision_hash,
                "approve",
                NodeExecutionId.for_node(
                    TRANSITIVE_RUN, workflow.revision_hash, "approve"
                ),
                WaitAnswerActor.OPERATOR,
                b'{"released": true}',
            ),
        )
        wait_for_state(started_runtime, RunState.COMPLETED, TRANSITIVE_RUN)

        recorded = github.recorded_pull_requests()
        assert len(recorded) == 1
        with started_runtime.engine.connect() as connection:
            intent = intent_snapshot_from_record(
                connection.execute(
                    sa.select(effect_intents).where(
                        effect_intents.c.run_id == TRANSITIVE_RUN.value
                    )
                )
                .mappings()
                .one()
            ).intent
    finally:
        started_runtime.close()

    assert body_carries_request_hash(
        recorded[0].body, intent.request.request_hash.value
    )
    assert TREE.decode("utf-8") in recorded[0].body
    assert REVIEWERS_VERDICT.decode("utf-8") not in recorded[0].body
